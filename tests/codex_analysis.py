"""Opt-in synthetic analysis via the installed release CLI; not pytest-collected.

Run: python3 tests/codex_analysis.py --run
Inputs, scripts and fetched outputs remain in ignored .local/workflows/analysis.
Only compact public-safe measurements are written to benchmarks/.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import shlex
import statistics
import subprocess
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
RSCRIPT = "/mnt/sdb/xzh/miniconda3/envs/tidy/bin/Rscript"
SEED = 20261002

ANALYSIS_R = r'''
suppressPackageStartupMessages(library(qs))
counts <- read.delim("counts.tsv", row.names=1, check.names=FALSE)
samples <- read.delim("samples.tsv", stringsAsFactors=FALSE)
stopifnot(identical(colnames(counts), samples$sample))
expression <- log2(as.matrix(counts) + 1)
control <- samples$group == "control"
case <- samples$group == "case"
results <- data.frame(
  gene=rownames(counts),
  mean_control=rowMeans(expression[,control]),
  mean_case=rowMeans(expression[,case]),
  log2_fold_change=rowMeans(expression[,case])-rowMeans(expression[,control]),
  p_value=apply(expression, 1, function(x) t.test(x[case], x[control])$p.value)
)
results$adjusted_p_value <- p.adjust(results$p_value, method="BH")
results$call <- ifelse(results$adjusted_p_value < 0.05 & abs(results$log2_fold_change) > 1,
                       ifelse(results$log2_fold_change > 0, "up", "down"), "unchanged")
summary <- do.call(rbind, lapply(c("control", "case"), function(group) {
  values <- colSums(counts[,samples$group == group])
  data.frame(group=group, samples=length(values), mean_library_size=mean(values),
             sd_library_size=sd(values), min_library_size=min(values), max_library_size=max(values))
}))
write.table(results, "differential.tsv", sep="\t", quote=FALSE, row.names=FALSE)
write.table(summary, "group_summary.tsv", sep="\t", quote=FALSE, row.names=FALSE)
qsave(list(counts=counts, samples=samples, expression=expression, results=results), "analysis.qs")
write.table(data.frame(component=c("R", "qs"), version=c(as.character(getRversion()),
            as.character(packageVersion("qs")))), "analysis_versions.tsv",
            sep="\t", quote=FALSE, row.names=FALSE)
cat(sprintf("analysis complete: %d genes, %d samples, %d up, %d down\n",
            nrow(counts), ncol(counts), sum(results$call == "up"), sum(results$call == "down")))
'''

PLOT_R = r'''
suppressPackageStartupMessages(library(qs))
suppressPackageStartupMessages(library(ggplot2))
cache <- qread("analysis.qs")
d <- cache$results
d$call <- factor(d$call, levels=c("down", "unchanged", "up"))
p <- ggplot(d, aes(log2_fold_change, -log10(adjusted_p_value), color=call)) +
  geom_point(size=1.9, alpha=0.85) +
  geom_vline(xintercept=c(-1, 1), linetype="dashed", color="grey65", linewidth=0.4) +
  geom_hline(yintercept=-log10(0.05), linetype="dashed", color="grey65", linewidth=0.4) +
  scale_color_manual(values=c(down="#247BA0", unchanged="#A5AAB2", up="#D1495B"), drop=FALSE) +
  labs(title="Synthetic expression analysis", subtitle="Seeded counts; 6 control and 6 case samples",
       x="Mean difference in log2(count + 1)", y="-log10(BH adjusted P)", color="Call") +
  theme_classic(base_size=12) + theme(legend.position="top")
ggsave("differential.png", p, width=7, height=5, dpi=180)
ggsave("differential.pdf", p, width=7, height=5)
write.table(data.frame(genes=nrow(cache$counts), samples=ncol(cache$counts),
                       up=sum(d$call == "up"), down=sum(d$call == "down"),
                       ggplot2_version=as.character(packageVersion("ggplot2"))),
            "cache_check.tsv", sep="\t", quote=FALSE, row.names=FALSE)
cat("child plot complete: qs cache loaded, PNG and PDF written\n")
'''


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly authorize the remote synthetic workflow")
    parser.add_argument("--cli", default=str(Path.home()/".local/bin/ssh4codex"))
    parser.add_argument("--server", default="solvinglab")
    parser.add_argument("--remote-root", help="Isolated remote analysis directory")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to execute the remote synthetic workflow")
    run_id = uuid.uuid4().hex[:12]
    remote = args.remote_root or f"/tmp/ssh4codex-codex-{run_id}/analysis"
    local = ROOT/".local/workflows/analysis"/run_id
    local.mkdir(parents=True)
    downloaded = local/"downloaded"
    started = time.perf_counter()
    requests = []
    tasks = []

    def cli(*argv):
        before = time.perf_counter()
        proc = subprocess.run([args.cli, *map(str, argv)], capture_output=True, timeout=180)
        requests.append({"command": argv[0], "seconds": round(time.perf_counter()-before, 3),
                         "stdout_bytes": len(proc.stdout), "stderr_bytes": len(proc.stderr),
                         "return_code": proc.returncode})
        if argv[0] == "--version":
            assert proc.returncode == 0
            return proc.stdout.decode().strip()
        result = json.loads(proc.stdout)
        if proc.returncode:
            raise RuntimeError(f"CLI {argv[0]} failed: {result.get('error', result.get('state'))}; task {result.get('task_id')}")
        return result

    def finish(result):
        while result["state"] in {"queued", "running"}:
            result = cli("wait", args.server, result["task_id"], "--seconds", "20", "--limit", "1024")
        assert result["state"] == "succeeded" and result["exit_code"] == 0
        assert result["session"] == "data"
        return result

    def run_task(label, script, artifacts=(), parent=None):
        tid = f"codex-analysis-{label}-{run_id}"
        before = time.perf_counter()
        argv = ["run", args.server, "--task-id", tid, "--cwd", remote,
                "--wait", "2", "--timeout", "120", "--limit", "1024"]
        if isinstance(script, Path):
            argv += ["--script", str(script), "--interpreter", RSCRIPT,
                     "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1"]
        else:
            argv += ["--command", script]
        for artifact in artifacts:
            argv += ["--artifact", artifact]
        result = finish(cli(*argv))
        tasks.append({"label": label, "task_id": tid, "parent_task_id": parent,
                      "state": result["state"], "exit_code": result["exit_code"],
                      "session": result["session"], "seconds": round(time.perf_counter()-before, 3)})
        return result

    def fetch_verified(result):
        fetched = cli("fetch", args.server, result["task_id"], "--to", downloaded)
        manifest = {Path(item["path"]).name: item for item in result["artifacts"]}
        verified = []
        for item in fetched["files"]:
            path = Path(item["path"])
            expected = manifest[path.name]
            digest = sha256(path)
            assert digest == item["sha256"] == expected["sha256"]
            assert path.stat().st_size == item["size"] == expected["size"]
            verified.append({"name": path.name, "remote_path": expected["path"],
                             "bytes": item["size"], "sha256": digest})
        assert len(verified) == len(manifest)
        return verified

    version = cli("--version")
    rng = random.Random(SEED)
    groups = ["control"]*6 + ["case"]*6
    sample_names = [f"sample_{i+1:02d}" for i in range(12)]
    counts = {}
    truth = {}
    for i in range(300):
        gene = f"gene_{i+1:04d}"
        effect = 3 if i < 20 else -3 if i < 40 else 0
        baseline = rng.uniform(300, 1200)
        counts[gene] = [max(1, round(baseline*2**(effect if group == "case" else 0)*
                                    math.exp(rng.gauss(0, 0.10)))) for group in groups]
        truth[gene] = "up" if effect > 0 else "down" if effect < 0 else "unchanged"
    with (local/"counts.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["gene", *sample_names])
        writer.writerows([gene, *values] for gene, values in counts.items())
    with (local/"samples.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["sample", "group"])
        writer.writerows(zip(sample_names, groups))
    (local/"analysis.R").write_text(ANALYSIS_R)
    (local/"plot.R").write_text(PLOT_R)
    run_task("prepare", "mkdir -p " + shlex.quote(remote))
    uploads = []
    for name in ("counts.tsv", "samples.tsv"):
        result = cli("put", args.server, local/name, remote+"/"+name)
        assert result["sha256"] == sha256(local/name)
        uploads.append({"name": name, "bytes": result["size"], "sha256": result["sha256"]})
    analysis = run_task("statistics", local/"analysis.R",
                        ("differential.tsv", "group_summary.tsv", "analysis.qs", "analysis_versions.tsv"))
    artifacts = fetch_verified(analysis)
    plot = run_task("plot", local/"plot.R", ("differential.png", "differential.pdf", "cache_check.tsv"),
                    parent=analysis["task_id"])
    artifacts += fetch_verified(plot)

    with (downloaded/"differential.tsv").open() as handle:
        results = list(csv.DictReader(handle, delimiter="\t"))
    assert len(results) == len(counts)
    max_error = 0
    calls = {"up": 0, "down": 0, "unchanged": 0}
    for row in results:
        values = [math.log2(x+1) for x in counts[row["gene"]]]
        expected = statistics.mean(values[6:])-statistics.mean(values[:6])
        max_error = max(max_error, abs(float(row["log2_fold_change"])-expected))
        assert row["call"] == truth[row["gene"]]
        calls[row["call"]] += 1
        assert 0 <= float(row["p_value"]) <= float(row["adjusted_p_value"]) <= 1
    assert max_error < 1e-10
    with (downloaded/"group_summary.tsv").open() as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            offset = 0 if row["group"] == "control" else 6
            totals = [sum(values[j] for values in counts.values()) for j in range(offset, offset+6)]
            assert int(row["samples"]) == 6
            assert abs(float(row["mean_library_size"])-statistics.mean(totals)) < 1e-7
            assert abs(float(row["sd_library_size"])-statistics.stdev(totals)) < 1e-7
    with (downloaded/"cache_check.tsv").open() as handle:
        cache = next(csv.DictReader(handle, delimiter="\t"))
    assert [int(cache[k]) for k in ("genes", "samples", "up", "down")] == [300, 12, 20, 20]
    assert (downloaded/"differential.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (downloaded/"differential.pdf").read_bytes()[:5] == b"%PDF-"
    report = {
        "scenario": "installed-release synthetic analysis and dependent plotting",
        "cli_version": version, "server_alias": args.server, "tmux_session": "data", "seed": SEED,
        "synthetic_dimensions": {"genes": 300, "samples": 12, "control": 6, "case": 6},
        "ground_truth": {"up": 20, "down": 20, "unchanged": 260, "effect_log2": 3},
        "checks": {"upload_sha256": True, "artifact_sha256_and_size": True,
                   "independent_fold_changes": True, "independent_group_statistics": True,
                   "all_ground_truth_calls": True, "parent_qs_cache_read": True,
                   "PNG_and_PDF_signatures": True},
        "observed_calls": calls, "max_fold_change_absolute_error": max_error,
        "all_passed": True, "elapsed_seconds": round(time.perf_counter()-started, 3),
        "subprocess_requests": len(requests),
        "returned_output_bytes": sum(r["stdout_bytes"]+r["stderr_bytes"] for r in requests),
        "primary_task_ids": [analysis["task_id"], plot["task_id"]],
        "remote_analysis_directory": remote,
        "requests": requests, "tasks": tasks, "uploads": uploads, "artifacts": artifacts,
    }
    (ROOT/"benchmarks/codex_analysis.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"all_passed": True, "elapsed_seconds": report["elapsed_seconds"],
                      "subprocess_requests": len(requests), "returned_output_bytes": report["returned_output_bytes"],
                      "local_outputs": str(local), "plot": str(downloaded/"differential.png")}))


if __name__ == "__main__":
    main()
