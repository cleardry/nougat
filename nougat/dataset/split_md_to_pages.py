"""
Copyright (c) Meta Platforms, Inc. and affiliates.

This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from operator import itemgetter
import re
from typing import Dict, List, Tuple, Union, Optional
import os
import fitz
from unidecode import unidecode
import Levenshtein

import numpy as np
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.linear_model import SGDClassifier

from nougat.dataset.staircase import Staircase
from nougat.dataset.splitter import (
    Splitter,
    get_first_last,
    get_glob_index,
)
from nougat.dataset.utils import unicode_to_latex, remove_pretty_linebreaks


class BagOfWords:
    def __init__(
        self,
        sentences: List[str],
        target: Optional[List[int]] = None,
    ) -> None:
        self.sentences = sentences
        self.target = target
        self.train()

    def train(self):
        if self.target is None:
            self.target = np.arange(len(self.sentences))
        self.count_vect = CountVectorizer()
        X_train_counts = self.count_vect.fit_transform(self.sentences)
        self.tfidf_transformer = TfidfTransformer(use_idf=True)
        X_train_tfidf = self.tfidf_transformer.fit_transform(X_train_counts)
        self.clf = SGDClassifier(
            loss="hinge",
            penalty="l2",
            alpha=1e-3,
            random_state=42,
            max_iter=5,
            tol=None,
        )
        self.clf.fit(X_train_tfidf, self.target)

    def __call__(
        self, text: Union[str, List[str]], lob_probs: bool = False
    ) -> np.ndarray:
        if type(text) == str:
            text = [text]
        X_new_counts = self.count_vect.transform(text)
        X_new_tfidf = self.tfidf_transformer.transform(X_new_counts)
        if lob_probs:
            return self.clf.predict_log_proba(X_new_tfidf)
        else:
            return self.clf.predict(X_new_tfidf)


def remove_short_seqs(seqs: List[str], minimum: int = 10) -> List[str]:
    out = []
    for seq in seqs:
        if len(seq) > minimum:
            out.append(seq)
    return out


def find_figures(
    pdf_pages: List[List[str]], figure_info: Union[Dict, List]
) -> List[Tuple[int, int]]:
    figure_locations = []
    iterator = figure_info.values() if type(figure_info) == dict else [figure_info]
    for figure_list in iterator:
        for i, f in enumerate(figure_list):
            if "caption" in f:
                fig_string = f["caption"]
            elif "text" in f:
                fig_string = f["text"]
            else:
                continue
            fig_string = unicode_to_latex(fig_string)
            if f["page"] >= len(pdf_pages):
                continue
            block, score = Splitter.fuzzysearch(
                "\n".join(pdf_pages[f["page"]]),
                fig_string,
            )
            if score > 0.8 and block[2] > 0:
                figure_locations.append((i, f["page"], block[0], block[2]))
    return figure_locations


def flatten(l: List) -> List:
    return [item for sublist in l for item in sublist]


def get_doc_text(
    doc: fitz.Document,
    splitn: bool = True,
    split_block: bool = True,
    minlen: Optional[int] = 10,
    return_blocks: bool = True,
) -> List[List[str]]:
    document_lines = []
    block_info = []
    for i, page in enumerate(doc.pages()):
        page_lines = []
        if split_block:
            blocks = page.get_text(
                "blocks", flags=fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_IMAGES
            )
        else:
            blocks = [page.get_text()]
        if return_blocks:
            for block in blocks:
                block_info.append(
                    {
                        "bbox": block[:4],
                        "page": i,
                        "type": "image" if block[-1] else "text",
                    }
                )

        for block in blocks:
            if block[-1] == 1:  # image
                continue
            block_text = block[-3] if split_block else block
            if not type(block_text) == str:
                continue
            if splitn:
                page_lines.extend(block_text.split("\n"))
            else:
                page_lines.append(block_text)
        if splitn:
            page_lines = remove_short_seqs(page_lines, minlen)
        document_lines.append(page_lines)
    if return_blocks:
        return document_lines, block_info
    return document_lines


def clean_pdf_text(pages: List[List[str]], num_words: int = 10) -> List[List[str]]:
    words = []
    for page in pages:
        first = get_first_last(
            " ".join(page).lower(), num_words=num_words, first_only=True
        )
        words.extend(first.split(" "))
    word_counts = Counter(words)
    common_words = [
        "the",
        "of",
        "a",
        "and",
        "to",
        "in",
        "is",
        "that",
        "for",
        "are",
        "this",
        "we",
        "figure",
        "fig.",
        "",
    ]
    frequent_words = []
    for w, f in word_counts.items():
        if w in common_words or w.startswith("\\"):
            continue
        if f / len(pages) >= 0.4:
            frequent_words.append(w)
    if len(frequent_words) == 0:
        return pages
    # remove frequent words from page beginning/end
    for i in range(len(pages)):
        page = pages[i]
        stop = 0
        page_num_words = 0
        for p in page:
            page_num_words += len(p.split(" "))
            stop += 1
            if page_num_words >= num_words:
                break
        for w in frequent_words:
            for j in range(stop):
                if w == "-":  # probably page number - \d -
                    pages[i][j] = re.sub(
                        r"-\s*\d{1,3}\s*-", "", pages[i][j], flags=re.IGNORECASE
                    )
                pages[i][j] = re.sub(re.escape(w), "", pages[i][j], flags=re.IGNORECASE)
    return pages


def split_markdown(
    doc: str,
    pdf: fitz.Document,
    figure_info: Optional[List[Dict]] = None,
    doc_fig: Dict[str, str] = {},
    minlen: int = 3,
    min_num_words: int = 22,
    doc_paragraph_chars: int = 1000,
    min_score: float = 0.75,
    staircase: bool = True,
) -> Tuple[List[str], Dict]:
    doc_paragraphs_full: List[str] = doc.split("\n")
    doc_paragraph_lengths = [len(p) for p in doc_paragraphs_full if len(p) > 1]
    num_lines = 1 + int(doc_paragraph_chars / np.mean(doc_paragraph_lengths))
    doc_paragraphs_full = [
        unidecode("\n".join(doc_paragraphs_full[i : i + num_lines]))
        for i in range(0, len(doc_paragraphs_full), num_lines)
    ]
    doc_paragraphs: List[str] = []
    doc_paragraph_indices: List[int] = []
    for i, p in enumerate(doc_paragraphs_full):
        if len(p) > 1:
            doc_paragraphs.append(
                re.sub(r"(\[(FOOTNOTE|FIGURE|TABLE).*?END\2\])", "", p)
            )
            doc_paragraph_indices.append(i)
    meta = {"pdffigures": figure_info}
    if len(pdf) > 1:
        pdf_text, block_info = get_doc_text(pdf, True, True, minlen)
        meta["mupdf"] = block_info
        pdf_content = [
            [unicode_to_latex(q).replace("\n", " ") for q in p if len(q) >= minlen]
            for p in pdf_text
        ]

        pdf_content = clean_pdf_text(pdf_content)
        if figure_info is not None:
            figure_locations = sorted(
                find_figures(pdf_content, figure_info), key=itemgetter(2), reverse=True
            )
            clean_pdf_content = deepcopy(pdf_content)
            for i, page_content in enumerate(pdf_content):
                len_sentences = np.cumsum([0] + [len(p) for p in page_content])
                for match in figure_locations:
                    _, page, start, len_ = match
                    if i != page:
                        continue
                    a, b = (
                        get_glob_index(len_sentences, start),
                        get_glob_index(len_sentences, start + len_) + 1,
                    )
                    for j, k in enumerate(range(a, b + 1)):
                        if len(clean_pdf_content[i]) == k:
                            break
                        if j == 0:
                            clean_pdf_content[i][k] = clean_pdf_content[i][k][
                                : start - len_sentences[k]
                            ]
                        elif k == b:
                            clean_pdf_content[i][k] = clean_pdf_content[i][k][
                                start + len_ - len_sentences[k] :
                            ]
                        else:
                            clean_pdf_content[i][k] = ""
                clean_pdf_content[i] = remove_short_seqs(clean_pdf_content[i], 0)
            pdf_content = clean_pdf_content
        paragraphs = flatten(pdf_content)
        num_paragraphs = np.cumsum([0] + [len(page) for page in pdf_content])
        if staircase:
            # train bag of words
            page_target = np.zeros(len(paragraphs))
            page_target[num_paragraphs[1:-1] - 1] = 1
            page_target = np.cumsum(page_target).astype(int)
            model = BagOfWords(paragraphs, target=page_target)
            labels = model(doc_paragraphs)

            # fit stair case function
            x = np.arange(len(labels))
            stairs = Staircase(len(labels), labels.max() + 1)
            stairs.fit(x, labels)
            boundaries = (stairs.get_boundaries().astype(int)).tolist()
            boundaries.insert(0, 0)
        else:
            boundaries = [0] * (len(pdf))
        splitter = Splitter(doc_paragraphs)
        pages = [(0, 0, 1.0)]
        meta["first_words"] = []
        meta["last_words"] = []
        for i in range(1, len(boundaries)):
            delta = (
                math.ceil(stairs.uncertainty[i - 1]) + 5
                if staircase
                else len(doc_paragraphs)
            )
            words_f = []
            words_l = []
            for p in pdf_content[i]:
                words_f.extend(p.split(" "))
                if len(words_f) >= min_num_words:
                    break
            for p in pdf_content[i - 1][::-1]:
                words_l.extend(p.split(" ")[::-1])
                if len(words_l) >= min_num_words:
                    words_l = words_l[::-1]
                    break
            if len(words_f) < 2:
                pages.append(pages[-1])
            first_words = " ".join(words_f[:min_num_words]).strip()
            last_words = " ".join(words_l[-min_num_words:]).strip()
            meta["first_words"].append(first_words)
            meta["last_words"].append(last_words)
            if len(first_words) < minlen and len(last_words) < minlen:
                pages.append(pages[-1])
                continue
            pages.append(
                splitter.split_first_last(
                    boundaries[i],
                    first_words,
                    last_words,
                    delta=delta,
                )
            )
    elif len(pdf) == 1:  # single page
        pages = [(0, 0, 1)]
    else:
        return
    pages.append((len(doc_paragraphs), -1, 1.0))
    out = []
    page_scores = {}
    for i in range(len(pages) - 1):
        score = (pages[i][2] + pages[i + 1][2]) * 0.5
        if score >= min_score:
            end = pages[i + 1][0]
            if end >= len(doc_paragraph_indices):
                end = None
            else:
                end = doc_paragraph_indices[pages[i + 1][0]] + 1
            lines = doc_paragraphs_full[doc_paragraph_indices[pages[i][0]] : end]
            if len(lines) > 0:
                lines[0] = lines[0][pages[i][1] :]
                lines[-1] = lines[-1][: pages[i + 1][1]]
        else:
            lines = []
        page_content = "\n".join(lines)
        page_content = remove_pretty_linebreaks(page_content)
        page_scores[i] = score
        out.append(page_content)

    meta["page_splits"] = pages
    meta["page_scores"] = page_scores
    meta["num_pages"] = len(pdf)

    # Reintroduce figures, tables and footnotes
    figure_tex = list(doc_fig.keys()), list(doc_fig.values())
    if len(doc_fig) > 0:
        iterator = figure_info.values() if type(figure_info) == dict else [figure_info]
        for figure_list in iterator:
            for i, f in enumerate(figure_list):
                if "caption" in f:
                    fig_string = f["caption"]
                elif "text" in f:
                    fig_string = f["text"]
                else:
                    continue
                ratios = []
                for tex in figure_tex[1]:
                    if f["figType"] == "Table":
                        tex = tex.partition(r"\end{table}")[2]
                    ratios.append(Levenshtein.ratio(tex, fig_string))
                k = np.argmax(ratios)
                if ratios[k] < 0.8:
                    continue
                if f["page"] < len(out) and out[f["page"]] != "":
                    out[f["page"]] += "\n\n" + remove_pretty_linebreaks(
                        figure_tex[1][k].strip()
                    )

    for i in range(len(out)):
        foot_match = re.findall(r"\[FOOTNOTE(.*?)\]\[ENDFOOTNOTE\]", out[i])
        for match in foot_match:
            out[i] = out[i].replace(
                "[FOOTNOTE%s][ENDFOOTNOTE]" % match,
                doc_fig.get("FOOTNOTE%s" % match, ""),
            )

        out[i] = re.sub(r"\[(FIGURE|TABLE)(.*?)\](.*?)\[END\1\]", "", out[i])
    return out, meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", type=str, help="Markdown file", required=True)
    parser.add_argument("--pdf", type=str, help="PDF File", required=True)
    parser.add_argument("--out", type=str, help="Out dir", required=True)
    parser.add_argument(
        "--figure",
        type=str,
        help="Figure info JSON",
    )
    parser.add_argument("--dpi", type=int, default=96)
    args = parser.parse_args()
    md = open(args.md, "r", encoding="utf-8").read().replace("\xa0", " ")
    pdf = fitz.open(args.pdf)
    try:
        fig_info = json.load(open(args.figure, "r", encoding="utf-8"))
    except FileNotFoundError:
        fig_info = None
    pages, meta = split_markdown(md, pdf, fig_info)
    if args.out:
        outpath = os.path.join(args.out, os.path.basename(args.pdf).partition(".")[0])
        os.makedirs(outpath, exist_ok=True)
        for i, content in enumerate(pages):
            if content:
                with open(
                    os.path.join(
                        outpath, "%02d_s=%.2f.mmd" % (i + 1, meta["page_scores"][i])
                    ),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(content)

                with open(os.path.join(outpath, "%02d.png" % (i + 1)), "wb") as f:
                    f.write(pdf[i].get_pixmap(dpi=args.dpi).pil_tobytes(format="PNG"))
