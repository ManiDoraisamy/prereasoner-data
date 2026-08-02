# Third-Party Models And Data

PreReasoner's source code is licensed under [Apache-2.0](LICENSE). That license does not replace the terms of
models, datasets, hosted services, or Python and JavaScript packages used with the project.

The principal runtime and training inputs are:

| Component | Use | Upstream terms |
|---|---|---|
| [Qwen2.5-0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B) | Base encoder for the trained LoRA adapter | Apache-2.0 |
| [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | Entity-resolution embeddings | MIT |
| [spaCy en_core_web_md](https://spacy.io/models/en#en_core_web_md) | English parsing and entity candidates | MIT; the installed wheel contains its license and source notices |
| [Wikidata](https://www.wikidata.org/wiki/Wikidata:Copyright) | Structured world knowledge and identifiers | Structured data is CC0; other Wikidata content can have different terms |
| [Schema.org](https://schema.org/docs/terms.html) | Property vocabulary used by the type model | CC BY-SA 3.0 |

Spider evaluation data is not distributed by this repository. Follow the dataset owner's terms when downloading it
through the instructions in `docs/SQL_AST.md`.

Python packages installed from the requirement files and browser libraries loaded by the frontend retain their
upstream licenses. Before publishing a model bundle or container image, preserve the notices shipped by those
dependencies and review the exact artifact set being distributed.
