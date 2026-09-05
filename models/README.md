# models/

Drop-in location for a local llama.cpp build and model file(s).

- `llama-server.exe` (or a subfolder containing it, e.g. an unzipped release)
- one main `.gguf` model file
- optionally a second `.gguf` draft/MTP model file (name containing "draft"
  or "mtp") for speculative decoding

`ghostwriter/config.py` checks this folder first (recursively) and uses
whatever it finds here in place of `config.yaml`'s `llama.server_exe` /
`llama.model_path` / `llama.draft_model_path`. If this folder is empty, the
paths configured in `config.yaml` are used unchanged - so nothing here is
required, it's just a convenient default location.
