"""
survivorship.py — Step 5: Apply survivorship rules to build one golden record from the matched set.

Per Tony Giordano's "Conforming Logic" (Slide 4 & 5):
- Survivorship rules:
    • Legal Name & DOB: System of Record (Hogan Core Banking KYC wins)
    • Mobile Phone: Most recently verified/active (TSYS/FDR card servicing wins)
    • Email: Most complete verified value (ACLS consumer loans wins)
    • Address: Statement system of record wins
- Produces:
    1. silver_customer (Golden Record with SCD Type 2 envelope)
    2. silver_customer_source_xref (Auditable lineage preserving every source key)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


class SurvivorshipEngine:
    # Priority hierarchies per Slide 4
    NAME_PRIORITY = ["HOGAN", "AFS", "ACLS", "TSYS"]
    DOB_PRIORITY = ["HOGAN", "ACLS", "TSYS", "AFS"]
    PHONE_PRIORITY = ["TSYS", "ACLS", "HOGAN", "AFS"]
    EMAIL_PRIORITY = ["ACLS", "AFS", "TSYS", "HOGAN"]
    ADDRESS_PRIORITY = ["HOGAN", "ACLS", "TSYS", "AFS"]

    def __init__(self):
        pass

    def _pick_by_priority(self, records: List[Dict[str, Any]], field: str, priority_list: List[str]) -> Tuple[Optional[Any], Optional[str]]:
        """Select field value based on source system precedence."""
        # Index records by source_system
        by_source = {r["source_system"]: r.get(field) for r in records if r.get(field)}
        for sys in priority_list:
            if sys in by_source and by_source[sys]:
                return by_source[sys], sys
        # Fallback to any non-null
        for r in records:
            if r.get(field):
                return r.get(field), r["source_system"]
        return None, None

    def merge_cluster(self, cluster: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        customer_key = cluster["customer_key"]
        member_wrappers = cluster["members"]
        raw_records = [mw["record"] for mw in member_wrappers]

        # ── ATTRIBUTE SURVIVORSHIP ─────────────────────────────────────────────
        legal_name, name_source = self._pick_by_priority(raw_records, "legal_name", self.NAME_PRIORITY)
        first_name, _ = self._pick_by_priority(raw_records, "first_name", self.NAME_PRIORITY)
        last_name, _ = self._pick_by_priority(raw_records, "last_name", self.NAME_PRIORITY)
        dob, _ = self._pick_by_priority(raw_records, "date_of_birth", self.DOB_PRIORITY)
        phone, _ = self._pick_by_priority(raw_records, "primary_phone_e164", self.PHONE_PRIORITY)
        email, _ = self._pick_by_priority(raw_records, "primary_email", self.EMAIL_PRIORITY)
        postal_code, _ = self._pick_by_priority(raw_records, "primary_postal_code", self.ADDRESS_PRIORITY)
        tax_id, _ = self._pick_by_priority(raw_records, "tax_id_normalized", self.NAME_PRIORITY)

        # Entity type: If commercial loan present -> check if individual guarantor or corporate
        types = [r.get("customer_type") for r in raw_records if r.get("customer_type")]
        customer_type = "ORGANIZATION" if "ORGANIZATION" in types and ("HOGAN" not in [r["source_system"] for r in raw_records]) else "INDIVIDUAL"

        # Status: ACTIVE takes precedence
        statuses = [r.get("relationship_status") for r in raw_records if r.get("relationship_status")]
        status = "ACTIVE" if "ACTIVE" in statuses else (statuses[0] if statuses else "UNKNOWN")

        contributing = sorted(list(set(r["source_system"] for r in raw_records)))
        now_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # Masked tax ID for privacy/governance
        masked_tax = f"***-**-{tax_id[-4:]}" if (tax_id and len(tax_id) == 9) else None

        # Golden Customer record
        golden_customer = {
            "customer_key": customer_key,
            "customer_type": customer_type,
            "legal_name": legal_name,
            "first_name": first_name,
            "last_name": last_name,
            "date_of_birth": dob,
            "tax_id_normalized": tax_id,
            "tax_id_masked": masked_tax,
            "primary_phone_e164": phone,
            "primary_email": email,
            "primary_postal_code": postal_code,
            "relationship_status": status,
            "survivorship_source": name_source or "UNKNOWN",
            "contributing_sources": contributing,
            "source_record_count": len(raw_records),
            "effective_from_ts": now_ts,
            "effective_to_ts": None,
            "is_current": True,
            "dq_status": "VALID",
        }

        # Cross-reference lineage entries (Slide 4)
        xref_entries: List[Dict[str, Any]] = []
        for mw in member_wrappers:
            rec = mw["record"]
            xref_entries.append({
                "xref_key": f"XREF-{uuid.uuid4().hex[:10].upper()}",
                "customer_key": customer_key,
                "source_system": rec["source_system"],
                "source_entity": rec["source_entity"],
                "source_customer_id": rec["source_customer_id"],
                "match_method": mw["match_method"],
                "match_score": mw["match_score"],
                "review_status": mw["review_status"],
                "linked_at": now_ts,
            })

        return golden_customer, xref_entries
