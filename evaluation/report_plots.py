"""Headless, exportable PNG/SVG figures; no network or GUI dependencies."""

from pathlib import Path


def plots(results, out: Path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from report_data import LEVELS

    plt.rcParams.update({"svg.hashsalt": "flowguard-report-v1", "font.size": 10})
    files = []

    def save(fig, name):
        if results["metadata"]["synthetic"]:
            fig.suptitle("SYNTHETIC DEMONSTRATION — not evaluation results", fontsize=11)
        elif results["metadata"].get("development"):
            fig.suptitle(
                "DEVELOPMENT DRY-RUN — partial observations, not final evaluation", fontsize=11
            )
        fig.tight_layout()
        for extension in ("png", "svg"):
            path = out / f"{name}.{extension}"
            metadata = {"Creator": "FlowGuard report"}
            if extension == "svg":
                metadata["Date"] = None
            fig.savefig(path, dpi=160, metadata=metadata)
            files.append(path.name)
        plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(13, 7))
    primary = results["metadata"]["primary_metrics"]
    for axis, metric in zip(axes.flat, primary, strict=False):
        for i, level in enumerate(LEVELS):
            item = results["ablation"][level]["primary"][metric]
            if item["estimate"] is not None:
                lo, hi = item["ci95"]
                value = item["estimate"]
                axis.errorbar(
                    i, value, yerr=[[max(0, value - lo)], [max(0, hi - value)]], fmt="o", capsize=4
                )
                axis.annotate(
                    f"n={item['n']}",
                    (i, value),
                    xytext=(0, 8),
                    textcoords="offset points",
                    ha="center",
                    fontsize=8,
                )
        axis.set(xticks=range(6), xticklabels=LEVELS, ylim=(-0.05, 1.15), title=metric)
        if not any(results["ablation"][level]["primary"][metric]["n"] for level in LEVELS):
            axis.text(
                0.5, 0.5, "No eligible measurements (n=0)", transform=axis.transAxes, ha="center"
            )
        axis.grid(axis="y", alpha=0.2)
    for axis in list(axes.flat)[len(primary) :]:
        axis.axis("off")
    save(fig, "ablation")

    fig, axis = plt.subplots(figsize=(8, 5))
    for item in results["autonomy_safety"]:
        autonomy, safety = item["autonomy"], item["safety"]
        if autonomy["estimate"] is None:
            continue
        x, y = autonomy["estimate"], safety["unapproved_high_risk"]
        lo, hi = autonomy["ci95"]
        axis.errorbar(x, y, xerr=[[max(0, x - lo)], [max(0, hi - x)]], fmt="o", capsize=4)
        axis.annotate(item["setting"], (x, y), xytext=(6, 8), textcoords="offset points")
    axis.axhline(0, color="grey", linestyle="--", linewidth=1)
    if not any(item["autonomy"]["n"] for item in results["autonomy_safety"]):
        axis.text(
            0.5, 0.5, "No recorded L5 outcomes/settings", transform=axis.transAxes, ha="center"
        )
    axis.set(
        xlabel="Autonomous validated resolution share (95% Wilson interval)",
        ylabel="Unapproved high-risk executions (count)",
        title="L5 autonomy versus safety",
    )
    save(fig, "autonomy-vs-safety")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for axis, family, metric in zip(
        axes, ("runtime", "release"), ("H1_recall", "bad_release_caught"), strict=True
    ):
        cases = [row for row in results["per_band"] if row["family"] == family]
        bands = sorted({row["band"] for row in cases})
        for level in LEVELS:
            xs, ys = [], []
            for i, band in enumerate(bands):
                row = next(
                    (row for row in cases if row["level"] == level and row["band"] == band), None
                )
                item = (
                    row["primary" if family == "runtime" else "secondary"][metric] if row else None
                )
                if item and item["estimate"] is not None:
                    xs.append(i)
                    ys.append(item["estimate"])
            if xs:
                axis.plot(xs, ys, "o-", label=level)
        axis.set(
            title=f"{family}: {metric}",
            xticks=range(len(bands)),
            xticklabels=bands,
            ylim=(-0.05, 1.05),
        )
        if axis.lines:
            axis.legend(fontsize=8)
        else:
            axis.text(0.5, 0.5, "No recorded outcomes", transform=axis.transAxes, ha="center")
    save(fig, "per-band")

    fig, axis = plt.subplots(figsize=(9, 5))
    labels, means, errors = [], [], []
    for row in results["llm_variance"]:
        item = row["summary"]["macro_f1"]
        if item["mean"] is not None:
            labels.append(f"{row['level']} / {row['setting']}")
            means.append(item["mean"])
            errors.append(item["sample_sd"] or 0)
    if means:
        axis.bar(range(len(means)), means, yerr=errors, capsize=4)
    else:
        axis.text(
            0.5,
            0.5,
            "No complete repeat triplets (expected subset: 60 items)",
            transform=axis.transAxes,
            ha="center",
        )
    axis.set(
        xticks=range(len(labels)),
        xticklabels=labels,
        ylim=(0, 1.1),
        ylabel="Macro-F1: repeat mean ± sample SD",
        title="LLM repeat-subset variance",
    )
    axis.tick_params(axis="x", rotation=20)
    save(fig, "llm-variance")
    return files
