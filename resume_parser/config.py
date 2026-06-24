import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model: str = ""
    concurrency: int = 5
    request_timeout: int = 60
    max_retries: int = 1
    text_truncate: int = 6000
    recursive: bool = False
    default_input_dir: str = ""
    default_output_dir: str = ""


def load_config(path: str | Path = "config.toml") -> Config:
    data: dict = {}
    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    env_key = os.environ.get("ARK_API_KEY")
    if env_key:
        cfg.ark_api_key = env_key
    return cfg
