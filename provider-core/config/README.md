# Sentinel/config directory

This placeholder keeps the `config/` directory in source control so Docker builds can copy it into the image. Runtime files like `sentinel.yaml` and `sentinel.env` are created/updated inside the container at `/app/config/`.

