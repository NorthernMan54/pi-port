# SPDX-License-Identifier: AGPL-3.0-or-later
"""Protect's known-faces DB (UpdateFaceDBRequest), and matching face embeddings against it.

The download is a tar of `known/uiface.json` (subject, instance and name maps) and
`known/uiface.db`. Protect enrols the embedding the device sent, so for pi-port the vectors
are SFace's 128 floats and compare directly with pi-port's own (docs/protect-face-matching.md).
`uiface.db` is read as 528-byte records, a 16-byte header then 128 float32, record i holding
instance i; that layout is inferred from one subject's DB.

Matching follows the AI Port firmware's ranking: per subject, coverage (the share of its
instances at or above the threshold), then the top similarity.
"""
import io
import json
import logging
import tarfile

import numpy as np

log = logging.getLogger("aiport-facedb")

DIM = 128
RECORD = 16 + 4 * DIM
HEADER = 16
NORM_RANGE = (1.0, 100.0)  # SFace features are unnormalised, ~5-20


def _pairs(doc, key):
    return {k: v for k, v in (doc.get(key) or [])}


class FaceDB:
    def __init__(self, instances):
        """instances: [(instance_id, subject_id, name, vector)]."""
        self.instances = instances
        self.subjects = {}
        for _iid, sid, name, _v in instances:
            self.subjects.setdefault(sid, name)
        self._vecs = (np.array([v / np.linalg.norm(v) for *_, v in instances])
                      if instances else np.zeros((0, DIM)))

    def __len__(self):
        return len(self.instances)

    def match(self, embedding, threshold):
        """(subject_id, name, similarity) of the best subject, or None below threshold."""
        if embedding is None or not self.instances:
            return None
        e = np.asarray(embedding, dtype=np.float64).ravel()
        n = np.linalg.norm(e)
        if e.shape[0] != DIM or not np.isfinite(n) or n == 0:
            return None
        sims = self._vecs @ (e / n)
        per = {}
        for (_iid, sid, _name, _v), sim in zip(self.instances, sims):
            hits, total, top = per.get(sid, (0, 0, -1.0))
            per[sid] = (hits + (sim >= threshold), total + 1, max(top, float(sim)))
        sid, (hits, total, top) = max(per.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[1][2]))
        if hits == 0:
            return None
        return sid, self.subjects[sid], top


def parse(tar_bytes):
    """FaceDB from the downloaded tar's bytes. Raises ValueError if it doesn't look right."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        files = {m.name.split("/")[-1]: tar.extractfile(m).read()
                 for m in tar.getmembers() if m.isfile()}
    if "uiface.json" not in files or "uiface.db" not in files:
        raise ValueError("no uiface.json/uiface.db in the face DB")
    doc = json.loads(files["uiface.json"])
    data = files["uiface.db"]
    to_subject = _pairs(doc, "InstanceIdToSubjectIdMap")
    names = _pairs(doc, "SubjectIdToNameMap")
    ignored_subjects = set(doc.get("IgnoredSubjectIds") or [])
    ignored = set(doc.get("IngnoredInstanceIds") or doc.get("IgnoredInstanceIds") or [])
    instances = []
    for iid, sid in sorted(to_subject.items()):
        if sid in ignored_subjects or iid in ignored:
            continue
        start = iid * RECORD + HEADER
        if iid < 0 or start + 4 * DIM > len(data):
            raise ValueError(f"instance {iid} is past the end of uiface.db ({len(data)} bytes)")
        vec = np.frombuffer(data[start:start + 4 * DIM], dtype="<f4").astype(np.float64)
        norm = np.linalg.norm(vec)
        if not np.all(np.isfinite(vec)) or not NORM_RANGE[0] <= norm <= NORM_RANGE[1]:
            raise ValueError(f"instance {iid} doesn't look like an embedding (norm {norm:.2f})")
        instances.append((iid, sid, names.get(sid) or "", vec))
    return FaceDB(instances)


def load(path):
    """FaceDB from a saved download, or None (logged) if it can't be read."""
    try:
        with open(path, "rb") as f:
            db = parse(f.read())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("face DB %s not used: %s", path, e)
        return None
    log.info("face DB %s: %d instance(s) of %d subject(s): %s", path, len(db),
             len(db.subjects), ", ".join(sorted(n or "?" for n in db.subjects.values())))
    return db
