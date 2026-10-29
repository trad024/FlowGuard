"""Fixed-seed Python 3.11 CPU fine-tuning on the complete verified train/validation export."""

import argparse
import hashlib
import json
import platform
import random
import sys
import time
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
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--unfreeze-layers", type=int, default=2)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("Python 3.11 is required")
    if not 1 <= args.epochs <= 10:
        raise SystemExit("epochs must be between 1 and 10")
    if not 1 <= args.threads <= 32 or not 1 <= args.batch_size <= 32:
        raise SystemExit("threads and batch size must be between 1 and 32")
    if not 1 <= args.unfreeze_layers <= 6:
        raise SystemExit("unfreeze-layers must be between 1 and 6")
    base = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    examples = load_examples(args.split, args.index, args.log_root, base["split_sha256"])
    train = [e for e in examples if e.split == "train"]
    validation = [e for e in examples if e.split == "validation"]
    if any({e.label for e in group} != set(range(len(CLASSES))) for group in (train, validation)):
        raise SystemExit("both training and validation must contain every class")
    if args.output.exists():
        raise SystemExit("output already exists; choose a new directory")
    print(
        f"Data verified: {len(train)} training logs, {len(validation)} validation logs. "
        f"CPU: {args.threads} threads; {args.epochs} epochs; batch size {args.batch_size}.",
        flush=True,
    )
    print("Loading training libraries...", flush=True)

    import psutil
    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(args.threads)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.use_deterministic_algorithms(True)
    print("Loading tokenizer (downloads on first use)...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(
        base["model"], revision=base["revision"], trust_remote_code=False
    )
    tokenizer.truncation_side = "left"
    print("Loading pinned base model (downloads on first use)...", flush=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        base["model"],
        revision=base["revision"],
        num_labels=len(CLASSES),
        id2label=dict(enumerate(CLASSES)),
        label2id={c: i for i, c in enumerate(CLASSES)},
        use_safetensors=True,
        trust_remote_code=False,
        attn_implementation="eager",
    ).to("cpu")
    # A small corpus does not justify updating all 66M parameters on a laptop CPU.
    # Retain the classifier and last two transformer blocks as the default trainable subset.
    for parameter in model.distilbert.parameters():
        parameter.requires_grad_(False)
    for layer in model.distilbert.transformer.layer[-args.unfreeze_layers :]:
        for parameter in layer.parameters():
            parameter.requires_grad_(True)

    def collate(batch):
        encoded = tokenizer(
            [e.text for e in batch],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded["labels"] = torch.tensor([e.label for e in batch], dtype=torch.long)
        return {k: v.to("cpu") for k, v in encoded.items()}

    train_loader = DataLoader(
        train,
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=collate,
        generator=torch.Generator().manual_seed(args.seed),
        num_workers=0,
    )
    val_loader = DataLoader(
        validation, batch_size=args.batch_size, shuffle=False, collate_fn=collate, num_workers=0
    )
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=2e-5, weight_decay=0.01
    )
    process = psutil.Process()
    started = time.perf_counter()
    args.output.mkdir(parents=True)
    checkpoint = args.output / "checkpoint"
    history, best = [], -1.0
    print(f"Model ready. Starting training: {len(train_loader)} batches per epoch.", flush=True)
    for epoch in range(1, args.epochs + 1):
        epoch_started = time.perf_counter()
        training_loss = 0.0
        model.train()
        for step, batch in enumerate(train_loader, 1):
            optimizer.zero_grad(set_to_none=True)
            loss = model(**batch).loss
            training_loss += loss.item() * len(batch["labels"])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            if step == 1 or step % 5 == 0 or step == len(train_loader):
                print(
                    f"Epoch {epoch}/{args.epochs} | batch {step}/{len(train_loader)} | "
                    f"loss {loss.item():.4f} | elapsed {time.perf_counter() - epoch_started:.1f}s",
                    flush=True,
                )
        print(f"Epoch {epoch}: validating...", flush=True)
        model.eval()
        predicted, probabilities = [], []
        with torch.inference_mode():
            for batch in val_loader:
                logits = model(**{k: v for k, v in batch.items() if k != "labels"}).logits
                predicted.extend(logits.argmax(dim=-1).cpu().tolist())
                probabilities.extend(logits.softmax(dim=-1).cpu().tolist())
        metrics = classification_metrics([e.label for e in validation], predicted)
        metrics.update(
            epoch=epoch,
            training_loss=training_loss / len(train),
            epoch_wall_s=time.perf_counter() - epoch_started,
        )
        history.append(metrics)
        (args.output / "training-log.json").write_text(
            json.dumps({"seed": args.seed, "device": "cpu", "epochs": history}, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(metrics), flush=True)
        if metrics["macro_f1"] > best:
            best = metrics["macro_f1"]
            model.save_pretrained(checkpoint, safe_serialization=True)
            tokenizer.save_pretrained(checkpoint)
            (args.output / "validation-metrics.json").write_text(json.dumps(metrics, indent=2))
            predictions = [
                {
                    "variant_id": e.variant,
                    "actual_root_cause": CLASSES[e.label],
                    "predicted_root_cause": CLASSES[
                        max(range(len(scores)), key=scores.__getitem__)
                    ],
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
        "device": "cpu",
        "cpu": platform.processor(),
        "platform": platform.platform(),
        "threads": args.threads,
        "batch_size": args.batch_size,
        "unfrozen_transformer_layers": args.unfreeze_layers,
        "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
        "training_wall_s": time.perf_counter() - started,
        "training_process_rss_mib": process.memory_info().rss / 2**20,
        "windows_peak_rss_mib": getattr(process.memory_info(), "peak_wset", 0) / 2**20 or None,
        "torch": torch.__version__,
        "index_sha256": canonical_hash(
            [{"variant": e.variant, "split": e.split, "log_sha256": e.sha256} for e in examples]
        ),
        "train_variants": [e.variant for e in train],
        "validation_variants": [e.variant for e in validation],
        "epochs": history,
        "best_validation_macro_f1": best,
        "best_epoch": max(history, key=lambda row: row["macro_f1"])["epoch"],
        "selection": "best validation macro-F1; no final-test evaluation",
    }
    (checkpoint / "encoder_metadata.json").write_text(json.dumps(metadata, indent=2))
    (args.output / "training-log.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
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
    print(f"Training complete. Results and weights: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
