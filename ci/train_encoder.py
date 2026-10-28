"""External Python 3.11 GPU training only. No CPU-training fallback or test-split access."""

import argparse
import hashlib
import json
import os
import random
import sys
import zipfile
from pathlib import Path

from encoder_data import canonical_hash, classification_metrics, load_examples
from flowguard.encoder import CLASSES

BASE_FILE = Path(__file__).with_name("encoder") / "base-model.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", type=Path, default=Path("evaluation/datasets/corpus-split.json"))
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("state/encoder-training"))
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("Select a Python 3.11 GPU runtime")
    if not 1 <= args.epochs <= 10:
        raise SystemExit("epochs must be between 1 and 10")
    base = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    examples = load_examples(args.split, args.index, args.log_root, base["split_sha256"])
    train = [e for e in examples if e.split == "train"]
    validation = [e for e in examples if e.split == "validation"]
    if any({e.label for e in group} != set(range(len(CLASSES))) for group in (train, validation)):
        raise SystemExit("both training and validation must contain every class")
    if args.output.exists():
        raise SystemExit("output already exists; choose a new directory")

    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if not torch.cuda.is_available():
        raise SystemExit("GPU required: run this script in the external free-GPU notebook")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    tokenizer = AutoTokenizer.from_pretrained(
        base["model"], revision=base["revision"], trust_remote_code=False
    )
    tokenizer.truncation_side = "left"
    model = AutoModelForSequenceClassification.from_pretrained(
        base["model"],
        revision=base["revision"],
        num_labels=len(CLASSES),
        id2label=dict(enumerate(CLASSES)),
        label2id={c: i for i, c in enumerate(CLASSES)},
        use_safetensors=True,
        trust_remote_code=False,
        attn_implementation="eager",
    ).to("cuda")

    def collate(batch):
        encoded = tokenizer(
            [e.text for e in batch],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded["labels"] = torch.tensor([e.label for e in batch], dtype=torch.long)
        return {k: v.to("cuda") for k, v in encoded.items()}

    train_loader = DataLoader(
        train,
        batch_size=8,
        shuffle=True,
        collate_fn=collate,
        generator=torch.Generator().manual_seed(args.seed),
        num_workers=0,
    )
    val_loader = DataLoader(
        validation, batch_size=8, shuffle=False, collate_fn=collate, num_workers=0
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5, weight_decay=0.01)
    args.output.mkdir(parents=True)
    checkpoint = args.output / "checkpoint"
    history, best = [], -1.0
    for epoch in range(1, args.epochs + 1):
        model.train()
        for batch in train_loader:
            optimizer.zero_grad(set_to_none=True)
            loss = model(**batch).loss
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        model.eval()
        predicted, probabilities = [], []
        with torch.inference_mode():
            for batch in val_loader:
                logits = model(**{k: v for k, v in batch.items() if k != "labels"}).logits
                predicted.extend(logits.argmax(dim=-1).cpu().tolist())
                probabilities.extend(logits.softmax(dim=-1).cpu().tolist())
        metrics = classification_metrics([e.label for e in validation], predicted)
        metrics["epoch"] = epoch
        history.append(metrics)
        print(json.dumps(metrics))
        if metrics["macro_f1"] > best:
            best = metrics["macro_f1"]
            model.save_pretrained(checkpoint, safe_serialization=True)
            tokenizer.save_pretrained(checkpoint)
            (args.output / "validation-metrics.json").write_text(json.dumps(metrics, indent=2))
            predictions = [
                {
                    "variant_id": e.variant,
                    "root_cause": CLASSES[e.label],
                    "scores": dict(zip(CLASSES, scores, strict=True)),
                }
                for e, scores in zip(validation, probabilities, strict=True)
            ]
            (args.output / "validation-predictions.json").write_text(
                json.dumps(predictions, indent=2)
            )
    metadata = {
        "base_model": base["model"],
        "revision": base["revision"],
        "seed": args.seed,
        "split_sha256": base["split_sha256"],
        "classes": list(CLASSES),
        "gpu": torch.cuda.get_device_name(),
        "torch": torch.__version__,
        "index_sha256": canonical_hash(
            [{"variant": e.variant, "split": e.split, "log_sha256": e.sha256} for e in examples]
        ),
        "train_variants": [e.variant for e in train],
        "validation_variants": [e.variant for e in validation],
        "epochs": history,
        "selection": "best validation macro-F1; no final-test evaluation",
    }
    (checkpoint / "encoder_metadata.json").write_text(json.dumps(metadata, indent=2))
    bundle = args.output / "encoder-weights.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(checkpoint.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
    with bundle.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    manifest = {"file": bundle.name, "sha256": digest, "size_bytes": bundle.stat().st_size}
    (args.output / "artifact.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
