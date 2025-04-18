from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import json

import numpy as np
import pandas as pd
import timm
import torch
from huggingface_hub import hf_hub_download
from huggingface_hub.utils import HfHubHTTPError
from PIL import Image
from simple_parsing import field, parse_known_args
from timm.data import create_transform, resolve_data_config
from torch import Tensor, nn
from torch.nn import functional as F

torch_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MODEL_REPO_MAP = {
    "vit": "SmilingWolf/wd-vit-tagger-v3",
    "swinv2": "SmilingWolf/wd-swinv2-tagger-v3",
    "convnext": "SmilingWolf/wd-convnext-tagger-v3",
}


def pil_ensure_rgb(image: Image.Image) -> Image.Image:
    if image.mode not in ["RGB", "RGBA"]:
        image = image.convert("RGBA") if "transparency" in image.info else image.convert("RGB")
    if image.mode == "RGBA":
        canvas = Image.new("RGBA", image.size, (255, 255, 255))
        canvas.alpha_composite(image)
        image = canvas.convert("RGB")
    return image


def pil_pad_square(image: Image.Image) -> Image.Image:
    w, h = image.size
    px = max(image.size)
    canvas = Image.new("RGB", (px, px), (255, 255, 255))
    canvas.paste(image, ((px - w) // 2, (px - h) // 2))
    return canvas


@dataclass
class LabelData:
    names: list[str]
    rating: list[np.int64]
    general: list[np.int64]
    character: list[np.int64]


def load_labels_hf(repo_id: str, revision: Optional[str] = None, token: Optional[str] = None) -> LabelData:
    try:
        csv_path = hf_hub_download(repo_id=repo_id, filename="selected_tags.csv", revision=revision, token=token)
        csv_path = Path(csv_path).resolve()
    except HfHubHTTPError as e:
        raise FileNotFoundError(f"selected_tags.csv failed to download from {repo_id}") from e

    df: pd.DataFrame = pd.read_csv(csv_path, usecols=["name", "category"])
    tag_data = LabelData(
        names=df["name"].tolist(),
        rating=list(np.where(df["category"] == 9)[0]),
        general=list(np.where(df["category"] == 0)[0]),
        character=list(np.where(df["category"] == 4)[0]),
    )

    return tag_data


def get_tags(probs: Tensor, labels: LabelData, gen_threshold: float, char_threshold: float):
    probs = list(zip(labels.names, probs.numpy()))

    rating_labels = dict([probs[i] for i in labels.rating])
    gen_labels = [probs[i] for i in labels.general]
    gen_labels = dict([x for x in gen_labels if x[1] > gen_threshold])
    gen_labels = dict(sorted(gen_labels.items(), key=lambda item: item[1], reverse=True))

    char_labels = [probs[i] for i in labels.character]
    char_labels = dict([x for x in char_labels if x[1] > char_threshold])
    char_labels = dict(sorted(char_labels.items(), key=lambda item: item[1], reverse=True))

    combined_names = [x for x in gen_labels]
    combined_names.extend([x for x in char_labels])

    caption = ", ".join(combined_names)
    taglist = caption.replace("_", " ").replace("(", "\(").replace(")", "\)")

    return caption, taglist, rating_labels, char_labels, gen_labels


@dataclass
class ScriptOptions:
    image_file: Path = field(positional=True)
    model: str = field(default="vit")
    gen_threshold: float = field(default=0.35)
    char_threshold: float = field(default=0.75)


def main(opts: ScriptOptions):
    repo_id = MODEL_REPO_MAP.get(opts.model)
    image_path = Path(opts.image_file).resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    print(f"Loading model '{opts.model}' from '{repo_id}'...")
    model: nn.Module = timm.create_model("hf-hub:" + repo_id).eval()
    state_dict = timm.models.load_state_dict_from_hf(repo_id)
    model.load_state_dict(state_dict)

    print("Loading tag list...")
    labels: LabelData = load_labels_hf(repo_id=repo_id)

    print("Creating data transform...")
    transform = create_transform(**resolve_data_config(model.pretrained_cfg, model=model))

    print("Loading image and preprocessing...")
    img_input: Image.Image = Image.open(image_path)
    img_input = pil_ensure_rgb(img_input)
    img_input = pil_pad_square(img_input)
    inputs: Tensor = transform(img_input).unsqueeze(0)
    inputs = inputs[:, [2, 1, 0]]

    print("Running inference...")
    with torch.inference_mode():
        if torch_device.type != "cpu":
            model = model.to(torch_device)
            inputs = inputs.to(torch_device)
        outputs = model.forward(inputs)
        outputs = F.sigmoid(outputs)
        if torch_device.type != "cpu":
            inputs = inputs.to("cpu")
            outputs = outputs.to("cpu")
            model = model.to("cpu")

    print("Processing results...")
    caption, taglist, ratings, character, general = get_tags(
        probs=outputs.squeeze(0),
        labels=labels,
        gen_threshold=opts.gen_threshold,
        char_threshold=opts.char_threshold,
    )

    results = {
        "caption": caption,
        "tags": taglist,
        "ratings": {k: float(v) for k, v in ratings.items()},
        "character_tags": {k: float(v) for k, v in character.items()},
        "general_tags": {k: float(v) for k, v in general.items()},
    }

    json_output = json.dumps(results, indent=4)
    print(json_output)

    with open("output.json", "w") as f:
        f.write(json_output)

    print("Done!")


if __name__ == "__main__":
    opts, _ = parse_known_args(ScriptOptions)
    if opts.model not in MODEL_REPO_MAP:
        print(f"Available models: {list(MODEL_REPO_MAP.keys())}")
        raise ValueError(f"Unknown model name '{opts.model}'")
    main(opts)
