"""The named reader: an encoder reads a sheet and a question into scores on named coordinates, and every decision after that reads only those names.

names.py     the name set and the closed heads
backbone.py  the one text encoder, bge-small or XLM-R base, shared by both sides
units.py     a table as units and edges, in an order that does not depend on the sheet's
model.py     relational attention (engine/encoder_model.py) over the units, one head of named coordinates
index.py     the compact per-tab index, cached by tab hash
links.py     the link score: weight x question coordinate x target coordinate, plus named rule evidence
reader.py    a question's reading on the program decoder's coordinates; the open channel proposes and never decides
chooser.py   the logistic model over named evidence, calibrated per form; answer or ask
pipeline.py  sheet, index, reader, decoder, verifier, chooser

Encoder only, float32, deterministic: scores are rounded before anything compares them. Nothing in production imports this package until the cut-over.
"""
