"""
standardizer.py — Step 4: Normalize formats, units, and codes before integrating.

Per Tony Giordano's "Conforming Logic" (Slide 3):
- Types & formats: Cast types, parse dates to ISO 8601, normalize phone to E.164, strip whitespace.
- Codes & reference data: Crosswalk each source code list to CDM controlled vocabulary via external reference tables.
- Preserves raw values alongside standardized values for auditability.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class Standardizer:
    def __init__(self, reference_dir: Optional[Path] = None):
        if reference_dir is None:
            reference_dir = Path(__file__).parent.parent / "reference"
        self.reference_dir = reference_dir
        self.customer_type_crosswalk = self._load_crosswalk(
            self.reference_dir / "ref_crosswalk_customer_type.csv"
        )
        self.status_crosswalk = self._load_crosswalk(
            self.reference_dir / "ref_crosswalk_status.csv"
        )

    def _load_crosswalk(self, path: Path) -> Dict[str, Dict[str, str]]:
        mapping: Dict[str, Dict[str, str]] = {}
        if not path.exists():
            return mapping
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sys_name = row["source_system"].strip().upper()
                src_code = row["source_code"].strip().upper()
                cdm_code = row["cdm_code"].strip().upper()
                if sys_name not in mapping:
                    mapping[sys_name] = {}
                mapping[sys_name][src_code] = cdm_code
        return mapping

    def lookup_customer_type(self, source_system: str, raw_code: str) -> str:
        sys_key = source_system.strip().upper()
        code_key = (raw_code or "").strip().upper()
        return self.customer_type_crosswalk.get(sys_key, {}).get(code_key, "INDIVIDUAL")

    def lookup_status(self, source_system: str, raw_code: str) -> str:
        sys_key = source_system.strip().upper()
        code_key = (raw_code or "").strip().upper()
        return self.status_crosswalk.get(sys_key, {}).get(code_key, "ACTIVE")

    @staticmethod
    def normalize_phone_e164(phone: Optional[str]) -> Optional[str]:
        """Normalize any US/international phone number to E.164 format (+1XXXXXXXXXX)."""
        if not phone:
            return None
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        if len(digits) > 7:
            return f"+{digits}"
        return None

    @staticmethod
    def parse_date_iso(raw_date: Optional[str]) -> Optional[str]:
        """Parse varying date formats (YYYYMMDD, MM/DD/YYYY, YYYY/MM/DD, YYYY-MM-DD) into ISO 8601."""
        if not raw_date:
            return None
        raw = str(raw_date).strip()
        for fmt in ("%Y%m%d", "%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Fallback if already ISO prefix
        if len(raw) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", raw):
            return raw[:10]
        return None

    @staticmethod
    def clean_tax_id(tax_id: Optional[str]) -> Optional[str]:
        """Strip punctuation and extract 9-digit national tax identifier / SSN / EIN."""
        if not tax_id:
            return None
        digits = re.sub(r"\D", "", str(tax_id))
        return digits if len(digits) == 9 else None

    @staticmethod
    def parse_name(name_str: Optional[str]) -> Dict[str, str]:
        """Normalize name and extract components (supports 'DOE, JANE M' and 'Jane M Doe')."""
        if not name_str:
            return {"legal_name": "", "first_name": "", "last_name": ""}
        s = str(name_str).strip()
        if "," in s:
            parts = [p.strip() for p in s.split(",", 1)]
            last = parts[0].title()
            rest = parts[1].split()
            first = rest[0].title() if rest else ""
            middle = rest[1].title() if len(rest) > 1 else ""
            full = f"{first} {middle} {last}".replace("  ", " ").strip()
            return {"legal_name": full, "first_name": first, "last_name": last}
        parts = s.split()
        if len(parts) == 1:
            return {"legal_name": parts[0].title(), "first_name": parts[0].title(), "last_name": ""}
        if len(parts) == 2:
            return {"legal_name": f"{parts[0].title()} {parts[1].title()}", "first_name": parts[0].title(), "last_name": parts[1].title()}
        first = parts[0].title()
        last = parts[-1].title()
        return {"legal_name": s.title(), "first_name": first, "last_name": last}

    def standardize_hogan(self, row: Dict[str, Any]) -> Dict[str, Any]:
        parsed = self.parse_name(row.get("CI_LEGAL_NAME"))
        return {
            "source_system": "HOGAN",
            "source_entity": "brz_hogan_ci_customer",
            "source_customer_id": row["CI_CUST_NUM"].strip(),
            "customer_type": self.lookup_customer_type("HOGAN", row.get("CI_CUST_TYPE", "IND")),
            "legal_name": parsed["legal_name"],
            "first_name": parsed["first_name"],
            "last_name": parsed["last_name"],
            "tax_id_normalized": self.clean_tax_id(row.get("CI_TAX_ID")),
            "date_of_birth": self.parse_date_iso(row.get("CI_BIRTH_DT")),
            "primary_phone_e164": self.normalize_phone_e164(row.get("CI_PHONE")),
            "primary_email": None,
            "primary_postal_code": row.get("CI_POST_CODE", "").strip()[:5] or None,
            "relationship_status": self.lookup_status("HOGAN", row.get("CI_STATUS_CD", "01")),
            "source_extract_ts": row.get("CI_EXTRACT_TS", "2026-01-10T08:00:00Z"),
            # Audit preserving raw inputs
            "raw_payload": row,
        }

    def standardize_afs(self, row: Dict[str, Any]) -> Dict[str, Any]:
        parsed = self.parse_name(row.get("PRIMARY_CONTACT_NAME"))
        return {
            "source_system": "AFS",
            "source_entity": "brz_afs_borrower",
            "source_customer_id": row["BORROWER_ID"].strip(),
            "customer_type": self.lookup_customer_type("AFS", row.get("BORROWER_CLASS", "COM")),
            "legal_name": row.get("LEGAL_ENTITY_NAME", "").strip().title(),
            "first_name": parsed["first_name"],
            "last_name": parsed["last_name"],
            "tax_id_normalized": self.clean_tax_id(row.get("TAX_EIN")),
            "date_of_birth": None,
            "primary_phone_e164": self.normalize_phone_e164(row.get("CONTACT_PHONE")),
            "primary_email": (row.get("CONTACT_EMAIL") or "").strip().lower() or None,
            "primary_postal_code": (row.get("BUSINESS_ADDRESS") or "").split()[-1] if row.get("BUSINESS_ADDRESS") else None,
            "relationship_status": self.lookup_status("AFS", row.get("STATUS", "A")),
            "source_extract_ts": f"{row.get('LAST_REVIEW_DATE', '2026-01-01')}T00:00:00Z",
            "raw_payload": row,
        }

    def standardize_tsys(self, row: Dict[str, Any]) -> Dict[str, Any]:
        parsed = self.parse_name(row.get("embossed_name"))
        return {
            "source_system": "TSYS",
            "source_entity": "brz_tsys_cardholder",
            "source_customer_id": row["cardholder_id"].strip(),
            "customer_type": self.lookup_customer_type("TSYS", row.get("card_type", "RETAIL")),
            "legal_name": parsed["legal_name"],
            "first_name": parsed["first_name"],
            "last_name": parsed["last_name"],
            "tax_id_normalized": self.clean_tax_id(row.get("tax_ssn")),
            "date_of_birth": None,
            "primary_phone_e164": self.normalize_phone_e164(row.get("mobile_phone")),
            "primary_email": None,
            "primary_postal_code": (row.get("billing_postal_code") or "").strip()[:5] or None,
            "relationship_status": self.lookup_status("TSYS", row.get("card_status", "OPEN")),
            "source_extract_ts": f"{row.get('last_active_date', '2026-10-01')}T00:00:00Z",
            "raw_payload": row,
        }

    def standardize_acls(self, row: Dict[str, Any]) -> Dict[str, Any]:
        first = (row.get("FIRST_NAME") or "").strip().title()
        last = (row.get("LAST_NAME") or "").strip().title()
        middle = (row.get("MIDDLE_NAME") or "").strip().title()
        full = f"{first} {middle} {last}".replace("  ", " ").strip()
        return {
            "source_system": "ACLS",
            "source_entity": "brz_acls_applicant",
            "source_customer_id": row["APPLICANT_ID"].strip(),
            "customer_type": "INDIVIDUAL",
            "legal_name": full,
            "first_name": first,
            "last_name": last,
            "tax_id_normalized": self.clean_tax_id(row.get("NATIONAL_ID")),
            "date_of_birth": self.parse_date_iso(row.get("DOB")),
            "primary_phone_e164": self.normalize_phone_e164(row.get("CELL_PHONE")),
            "primary_email": (row.get("EMAIL") or "").strip().lower() or None,
            "primary_postal_code": (row.get("POSTAL_CODE") or "").strip()[:5] or None,
            "relationship_status": self.lookup_status("ACLS", row.get("APP_STATUS", "A")),
            "source_extract_ts": f"{row.get('APP_DATE', '2026-03-20')}T00:00:00Z",
            "raw_payload": row,
        }
