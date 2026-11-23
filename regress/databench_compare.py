"""databench_eval's Evaluator.default_compare, copied verbatim (a method made a function) from
github.com/jorses/databench_eval src/databench_eval/eval.py at commit 65f92cfad4f986f5496170f6bbcee48323f56c2f
(2025-05-20), MIT License, Copyright (c) 2024 Jorge Oses Grijalba: the official DataBench (SemEval-2025 Task 8)
comparison of one response against its truth, per answer type."""
# ruff: noqa: F841  (the release's code kept as written)
import math

import numpy as np
import pandas as pd


def default_compare(value, truth, semantic):
    STRIP_CHARS = "[]'\" "
    semantic = semantic.strip()
    valid_null_set = [None, "nan", "", " ", np.nan, "np.nan", "None"]

    if str(value).strip(STRIP_CHARS) in valid_null_set and str(truth).strip(STRIP_CHARS) in valid_null_set:
        return True
    if str(value).strip(STRIP_CHARS) in valid_null_set or str(truth).strip(STRIP_CHARS) in valid_null_set:
        return False

    if semantic == "boolean":
        valid_true_values = ['true', 'yes', 'y']
        valid_false_values = ['false', 'no', 'n']
        value_str = str(value).strip(STRIP_CHARS).lower()
        truth_str = str(truth).strip(STRIP_CHARS).lower()
        return (value_str in valid_true_values and truth_str in valid_true_values) or (value_str in valid_false_values and truth_str in valid_false_values)
    elif semantic == "category":
        value_str = str(value).strip(STRIP_CHARS)
        truth_str = str(truth).strip(STRIP_CHARS)
        if value_str == truth_str:
            return True

        try:
            value_date = pd.to_datetime(value_str).date()
            truth_date = pd.to_datetime(truth_str).date()
            return value_date == truth_date
        except (ValueError, TypeError):
            if not value_str and not truth_str:
                return True
            return value_str == truth_str
    elif semantic == "number":
        try:
            value_cleaned = ''.join(char for char in str(value) if char.isdigit() or char in ['.', '-'])
            truth_cleaned = ''.join(char for char in str(truth) if char.isdigit() or char in ['.', '-'])
            return math.trunc(float(value_cleaned) * 100) == math.trunc(float(truth_cleaned) * 100)
        except:
            return False
    elif semantic == "list[category]":
        try:
            value_list = [item.strip(STRIP_CHARS) for item in str(value).strip('[]').split(',')]
            truth_list = [item.strip(STRIP_CHARS) for item in str(truth).strip('[]').split(',')]
            value_list = [
                v if v not in valid_null_set else ""
                for v in value_list
            ]
            truth_list = [
                t if t not in valid_null_set else "" for t in truth_list
            ]
            if len(value_list) != len(truth_list):
                return False

            # Attempt to parse each item as a date
            try:
                value_dates = [pd.to_datetime(item).date() for item in value_list]
                truth_dates = [pd.to_datetime(item).date() for item in truth_list]
                return set(value_dates) == set(truth_dates)
            except (ValueError, TypeError):
                # If parsing as dates fails, compare as strings
                return set(value_list) == set(truth_list)
        except Exception as exc:
            return False
    elif semantic == "list[number]":
        try:
            value_list = sorted(float(''.join(c for c in v.strip() if c.isdigit() or c in ['.', '-'])) for v in str(value).strip('[]').split(',') if v.strip())
            truth_list = sorted(float(''.join(c for c in t.strip() if c.isdigit() or c in ['.', '-'])) for t in str(truth).strip('[]').split(',') if t.strip())

            value_list = [int(v * 100) / 100 for v in value_list]
            truth_list = [int(t * 100) / 100 for t in truth_list]

            if len(value_list) != len(truth_list):
                return False

            return set(value_list) == set(truth_list)
        except Exception as exc:
            return False
    else:
        raise Exception(f"Semantic not supported: {semantic}")
