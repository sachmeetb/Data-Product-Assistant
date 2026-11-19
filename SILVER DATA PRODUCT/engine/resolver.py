"""
resolver.py — Step 5: Match records across sources so one entity gets one key.

Per Tony Giordano's "Conforming Logic" (Slide 4):
- Block: Group likely candidates (same postcode, soundex, tax ID) to limit comparisons.
- Match: Deterministic rules (exact tax ID / SSN) plus composite/probabilistic scoring (DOB + Phone).
- Cluster: Assign a persistent CDM surrogate key and track all source keys.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple


class DisjointSet:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, item: str) -> str:
        if item not in self.parent:
            self.parent[item] = item
            return item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: str, b: str) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


class IdentityResolver:
    def __init__(self):
        self.steward_review_queue: List[Dict[str, Any]] = []

    def resolve(self, standardized_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Takes standardized records from multiple sources and groups them into resolved customer clusters.
        Also populates `self.steward_review_queue` with Tier-3 probabilistic match candidates.
        """
        self.steward_review_queue = []
        now_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # Assign a local unique ID per source record
        record_map: Dict[str, Dict[str, Any]] = {}
        for r in standardized_records:
            rec_id = f"{r['source_system']}:{r.get('source_entity', '')}:{r['source_customer_id']}"
            r["_internal_id"] = rec_id
            record_map[rec_id] = r

        dset = DisjointSet()
        matches: Dict[Tuple[str, str], Dict[str, Any]] = {}
        fuzzy_held_ids: Set[str] = set()

        # ── BLOCKING & COMPARISONS ────────────────────────────────────────────
        record_list = list(record_map.values())
        n = len(record_list)

        for i in range(n):
            for j in range(i + 1, n):
                r1 = record_list[i]
                r2 = record_list[j]
                id1 = r1["_internal_id"]
                id2 = r2["_internal_id"]

                matched, rule_name, score = self._evaluate_match(r1, r2)
                if matched:
                    dset.union(id1, id2)
                    matches[(id1, id2)] = {
                        "rule": rule_name,
                        "score": score,
                    }
                else:
                    fuzzy_ok, fuzzy_rule, fuzzy_score = self._evaluate_fuzzy_match(r1, r2)
                    if fuzzy_ok:
                        self.steward_review_queue.append({
                            "review_id": f"REV-{uuid.uuid4().hex[:8].upper()}",
                            "candidate_a_system": r1["source_system"],
                            "candidate_a_entity": r1.get("source_entity", ""),
                            "candidate_a_id": r1["source_customer_id"],
                            "candidate_a_name": r1.get("legal_name", ""),
                            "candidate_b_system": r2["source_system"],
                            "candidate_b_entity": r2.get("source_entity", ""),
                            "candidate_b_id": r2["source_customer_id"],
                            "candidate_b_name": r2.get("legal_name", ""),
                            "match_rule": fuzzy_rule,
                            "similarity_score": round(fuzzy_score, 2),
                            "shared_blocking_key": r1.get("primary_postal_code") or "",
                            "review_status": "PENDING_STEWARD_REVIEW",
                            "created_at": now_ts,
                        })
                        if r1.get("table_role") == "SECONDARY_PARTY_SOURCE":
                            fuzzy_held_ids.add(id1)
                        if r2.get("table_role") == "SECONDARY_PARTY_SOURCE":
                            fuzzy_held_ids.add(id2)

        # ── GROUP BY ROOT PARENT ──────────────────────────────────────────────
        clusters: Dict[str, List[Dict[str, Any]]] = {}
        for rec_id, r in record_map.items():
            root = dset.find(rec_id)
            if root == rec_id and rec_id in fuzzy_held_ids:
                # Secondary party record held in Steward Review Queue until adjudicated
                continue
            if root not in clusters:
                clusters[root] = []
            clusters[root].append(r)

        # ── FORM CONSOLIDATED CLUSTERS WITH DETERMINISTIC SURROGATE KEY ───────
        resolved_clusters: List[Dict[str, Any]] = []
        for root, members in clusters.items():
            # Derive deterministic surrogate customer_key
            # Priority: clean tax ID if available, else sorted primary source keys
            tax_ids = sorted([m["tax_id_normalized"] for m in members if m.get("tax_id_normalized")])
            if tax_ids:
                seed = f"TAX:{tax_ids[0]}"
            else:
                member_keys = sorted([f"{m['source_system']}:{m['source_customer_id']}" for m in members])
                seed = f"KEYS:{'|'.join(member_keys)}"

            hash_digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12].upper()
            customer_key = f"CUST-{hash_digest}"

            # Determine match method & score for each member
            member_details: List[Dict[str, Any]] = []
            for m in members:
                m_id = m["_internal_id"]
                match_method = "PRIMARY_ENROLLMENT"
                match_score = 1.00
                review_status = "AUTO_APPROVED"

                for (a, b), meta in matches.items():
                    if (m_id == a or m_id == b) and (dset.find(a) == root):
                        match_method = meta["rule"]
                        match_score = meta["score"]
                        break

                member_details.append({
                    "record": m,
                    "match_method": match_method,
                    "match_score": match_score,
                    "review_status": review_status,
                })

            resolved_clusters.append({
                "cluster_root": root,
                "customer_key": customer_key,
                "members": member_details,
            })

        return resolved_clusters

    def _evaluate_fuzzy_match(self, r1: Dict[str, Any], r2: Dict[str, Any]) -> Tuple[bool, str, float]:
        """
        Tier-3 Probabilistic Match (routed to Steward Review Queue instead of auto-linking):
        Same 5-digit postal code + fuzzy last name similarity >= 0.80.
        """
        zip1 = (r1.get("primary_postal_code") or "").strip()[:5]
        zip2 = (r2.get("primary_postal_code") or "").strip()[:5]
        last1 = (r1.get("last_name") or "").strip().lower()
        last2 = (r2.get("last_name") or "").strip().lower()
        if not zip1 or not zip2 or zip1 != zip2 or not last1 or not last2:
            return False, "NO_MATCH", 0.0
        sim = SequenceMatcher(None, last1, last2).ratio()
        if sim >= 0.80:
            return True, "R3_FUZZY_NAME_POSTAL", round(0.80 + (sim - 0.80) * 0.6, 2)
        return False, "NO_MATCH", 0.0

    def _evaluate_match(self, r1: Dict[str, Any], r2: Dict[str, Any]) -> Tuple[bool, str, float]:
        """
        Tiered matching rules per Slide 4:
        - Tier 1: Deterministic Tax ID (SSN/EIN) Match -> Score 1.00
        - Tier 2: Composite DOB + Phone Match -> Score 0.95
        - Tier 3: High-confidence Name + Phone Match -> Score 0.90
        """
        # Rule 1: Exact Tax ID / SSN
        tax1 = r1.get("tax_id_normalized")
        tax2 = r2.get("tax_id_normalized")
        if tax1 and tax2 and tax1 == tax2:
            return True, "EXACT_TAX_ID", 1.00

        # Rule 2: Exact DOB + Exact E.164 Phone
        dob1 = r1.get("date_of_birth")
        dob2 = r2.get("date_of_birth")
        phone1 = r1.get("primary_phone_e164")
        phone2 = r2.get("primary_phone_e164")
        if dob1 and dob2 and phone1 and phone2 and dob1 == dob2 and phone1 == phone2:
            return True, "COMPOSITE_PHONE_DOB", 0.95

        # Rule 3: Last Name Match + Exact E.164 Phone
        last1 = (r1.get("last_name") or "").strip().lower()
        last2 = (r2.get("last_name") or "").strip().lower()
        if last1 and last2 and phone1 and phone2 and last1 == last2 and phone1 == phone2:
            return True, "COMPOSITE_LASTNAME_PHONE", 0.90

        return False, "NO_MATCH", 0.00
