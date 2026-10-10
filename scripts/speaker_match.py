#!/usr/bin/env python3
"""Find people who submitted under more than one identity by comparing voiceprints.

  enroll <person_id> <audio...>   add recordings for a person
  check  <audio>                  compare a new recording against everyone enrolled
  scan                            compare everyone against everyone
  list                            show who is enrolled

Embeddings are computed locally; audio never leaves this machine.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio

SAMPLE_RATE = 16000
CHUNK_SECONDS = 20
MIN_SECONDS = 3
MODEL = "speechbrain/spkrec-ecapa-voxceleb"
CACHE = Path.home() / ".cache" / "voice-verifier"

_encoder = None


def encoder():
    global _encoder
    if _encoder is None:
        from speechbrain.inference.speaker import EncoderClassifier

        _encoder = EncoderClassifier.from_hparams(source=MODEL, savedir=str(CACHE / "ecapa"))
    return _encoder


def load_audio(path: Path) -> torch.Tensor:
    """Return mono 16 kHz samples. Falls back to ffmpeg for formats libsndfile can't read."""
    try:
        samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:
        if not shutil.which("ffmpeg"):
            raise SystemExit(f"{path}: unsupported format. Install ffmpeg or convert to wav first.")
        wav = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", "-"],
            check=True,
            capture_output=True,
        ).stdout
        samples, rate = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    audio = torch.from_numpy(samples).mean(dim=1)
    if rate != SAMPLE_RATE:
        audio = torchaudio.functional.resample(audio, rate, SAMPLE_RATE)
    return audio


def normalize(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def embed(path: Path) -> np.ndarray:
    audio = load_audio(path)
    seconds = len(audio) / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        print(f"warning: {path} is only {seconds:.1f}s; scores for short clips are unreliable", file=sys.stderr)
    chunks = [c for c in audio.split(CHUNK_SECONDS * SAMPLE_RATE) if len(c) >= SAMPLE_RATE] or [audio]
    with torch.no_grad():
        vectors = [encoder().encode_batch(c.unsqueeze(0)).squeeze().numpy() for c in chunks]
    return normalize(np.mean([normalize(v) for v in vectors], axis=0))


class Store:
    """One .npy per person holding an [n_clips, dim] array, plus sources.json for traceability."""

    def __init__(self, root: Path):
        self.root = root
        self.sources_file = root / "sources.json"

    def people(self) -> dict[str, np.ndarray]:
        return {p.stem: np.load(p) for p in sorted(self.root.glob("*.npy"))}

    def centroids(self) -> dict[str, np.ndarray]:
        return {pid: normalize(clips.mean(axis=0)) for pid, clips in self.people().items()}

    def add(self, person_id: str, vectors: list[np.ndarray], files: list[Path]) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{person_id}.npy"
        clips = np.vstack(([np.load(target)] if target.exists() else []) + [np.stack(vectors)])
        np.save(target, clips)
        sources = json.loads(self.sources_file.read_text()) if self.sources_file.exists() else {}
        sources.setdefault(person_id, []).extend(str(f) for f in files)
        self.sources_file.write_text(json.dumps(sources, indent=2, ensure_ascii=False))
        return len(clips)


def verdict(score: float, args: argparse.Namespace) -> str:
    if score >= args.match:
        return "MATCH"
    if score >= args.review:
        return "REVIEW"
    return "-"


def cmd_enroll(args: argparse.Namespace, store: Store) -> int:
    if "/" in args.person_id or args.person_id == "sources":
        raise SystemExit("person_id must be a plain identifier")
    total = store.add(args.person_id, [embed(f) for f in args.files], args.files)
    print(f"{args.person_id}: {total} clip(s) enrolled")
    return 0


def cmd_check(args: argparse.Namespace, store: Store) -> int:
    centroids = store.centroids()
    if not centroids:
        print("Nobody is enrolled yet.")
        return 0
    vector = embed(args.file)
    ranked = sorted(((float(vector @ c), pid) for pid, c in centroids.items()), reverse=True)
    best = ranked[0][0]
    summary = {"MATCH": "MATCH: likely an enrolled person", "REVIEW": "REVIEW: listen to both recordings", "-": "no match"}
    print(f"{summary[verdict(best, args)]}  ({args.file}, {len(centroids)} enrolled)")
    for score, pid in ranked[: args.top]:
        print(f"    {score:6.3f}  {verdict(score, args):<7} {pid}")
    return 0


def cmd_scan(args: argparse.Namespace, store: Store) -> int:
    centroids = store.centroids()
    if len(centroids) < 2:
        print("Need at least two enrolled people.")
        return 0
    pairs = sorted(((float(centroids[a] @ centroids[b]), a, b) for a, b in combinations(centroids, 2)), reverse=True)
    hits = [p for p in pairs if p[0] >= args.review]
    print(f"{len(centroids)} people, {len(pairs)} pairs, {len(hits)} at or above review threshold {args.review}")
    for score, a, b in hits:
        print(f"    {score:6.3f}  {verdict(score, args):<7} {a}  <->  {b}")

    # Almost every pair is two different people, so this distribution is the
    # impostor distribution for this data: set --match above its upper tail.
    scores = np.array([p[0] for p in pairs])
    print("\nscore distribution across all pairs (use the upper tail to tune --match / --review):")
    for q in (50, 90, 99, 99.9):
        print(f"    p{q:<5} {np.percentile(scores, q):6.3f}")
    return 0


def cmd_list(args: argparse.Namespace, store: Store) -> int:
    for pid, clips in store.people().items():
        print(f"{pid}\t{len(clips)} clip(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("voiceprints"), help="voiceprint directory (default: ./voiceprints)")
    parser.add_argument("--match", type=float, default=0.50, help="cosine score at or above which two voices are treated as the same person")
    parser.add_argument("--review", type=float, default=0.30, help="cosine score at or above which a human should compare the recordings")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enroll")
    p.add_argument("person_id")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("check")
    p.add_argument("file", type=Path)
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(func=cmd_check)

    sub.add_parser("scan").set_defaults(func=cmd_scan)
    sub.add_parser("list").set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args, Store(args.db))


if __name__ == "__main__":
    sys.exit(main())
