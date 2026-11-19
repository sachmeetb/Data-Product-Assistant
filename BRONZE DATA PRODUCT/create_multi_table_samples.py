"""
create_multi_table_samples.py — Generates 1-to-many multi-table Bronze sample datasets
for Banking (Hogan, AFS, TSYS, ACLS) and Industrial Water Account 360 (SAP, 3D TRASAR IoT, Salesforce FSL).

Proves that a source system is modeled as 1-to-many datasets (Batch + Kafka Streaming)
feeding multiple Silver domain tables.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def generate_all() -> None:
    root = Path(__file__).resolve().parent.parent
    brz_4_dir = root / "BRONZE DATA PRODUCT" / "samples" / "banking_4_sources"
    brz_multi = root / "BRONZE DATA PRODUCT" / "samples" / "banking_multi_table"
    slv_multi = root / "SILVER DATA PRODUCT" / "samples" / "banking_multi_table"

    brz_multi.mkdir(parents=True, exist_ok=True)
    slv_multi.mkdir(parents=True, exist_ok=True)

    # 1. Copy the 4 base party tables so primary keys & record counts stay 100% compatible
    for base_csv in (
        "brz_hogan_ci_customer.csv",
        "brz_afs_borrower.csv",
        "brz_tsys_cardholder.csv",
        "brz_acls_applicant.csv",
    ):
        src = brz_4_dir / base_csv
        if src.exists():
            shutil.copy2(src, brz_multi / base_csv)

    # 2. HOGAN Child Tables (1:N per CI_CUST_NUM)
    hogan_dd_accounts = [
        {
            "DD_ACCT_NUM": "DDA-900101",
            "DD_CUST_NUM": "HOG-100291",
            "DD_PROD_TYPE": "CHKG",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20190315",
            "DD_CURR_BAL": "48250.75",
            "DD_AVAIL_BAL": "47900.75",
            "DD_BRANCH_NUM": "0101",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900102",
            "DD_CUST_NUM": "HOG-100291",
            "DD_PROD_TYPE": "MMDA",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20210720",
            "DD_CURR_BAL": "185000.00",
            "DD_AVAIL_BAL": "185000.00",
            "DD_BRANCH_NUM": "0101",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900201",
            "DD_CUST_NUM": "HOG-204510",
            "DD_PROD_TYPE": "CHKG",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20180510",
            "DD_CURR_BAL": "64120.40",
            "DD_AVAIL_BAL": "64120.40",
            "DD_BRANCH_NUM": "0204",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900301",
            "DD_CUST_NUM": "HOG-554411",
            "DD_PROD_TYPE": "SAVG",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20161102",
            "DD_CURR_BAL": "19450.00",
            "DD_AVAIL_BAL": "19450.00",
            "DD_BRANCH_NUM": "0301",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900401",
            "DD_CUST_NUM": "HOG-991204",
            "DD_PROD_TYPE": "CHKG",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20220118",
            "DD_CURR_BAL": "31200.00",
            "DD_AVAIL_BAL": "30800.00",
            "DD_BRANCH_NUM": "0402",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900402",
            "DD_CUST_NUM": "HOG-991204",
            "DD_PROD_TYPE": "SAVG",
            "DD_STATUS_CD": "01",
            "DD_OPEN_DT": "20230410",
            "DD_CURR_BAL": "92000.00",
            "DD_AVAIL_BAL": "92000.00",
            "DD_BRANCH_NUM": "0402",
            "DD_CURRENCY": "USD",
        },
        {
            "DD_ACCT_NUM": "DDA-900501",
            "DD_CUST_NUM": "HOG-332190",
            "DD_PROD_TYPE": "CHKG",
            "DD_STATUS_CD": "05",
            "DD_OPEN_DT": "20100901",
            "DD_CURR_BAL": "12500.00",
            "DD_AVAIL_BAL": "12500.00",
            "DD_BRANCH_NUM": "0105",
            "DD_CURRENCY": "USD",
        },
    ]
    write_csv(brz_multi / "brz_hogan_dd_account.csv", hogan_dd_accounts)

    hogan_ci_addresses = [
        {
            "CI_ADDR_ID": "HADDR-01",
            "CI_CUST_NUM": "HOG-100291",
            "ADDR_USAGE_CD": "RES",
            "STREET_LINE_1": "100 N Michigan Ave Apt 4B",
            "CITY_NAME": "Chicago",
            "STATE_CD": "IL",
            "POSTAL_CD": "60601",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20250615",
        },
        {
            "CI_ADDR_ID": "HADDR-02",
            "CI_CUST_NUM": "HOG-100291",
            "ADDR_USAGE_CD": "STMT",
            "STREET_LINE_1": "200 W Adams St Suite 1800",
            "CITY_NAME": "Chicago",
            "STATE_CD": "IL",
            "POSTAL_CD": "60606",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20250901",
        },
        {
            "CI_ADDR_ID": "HADDR-03",
            "CI_CUST_NUM": "HOG-204510",
            "ADDR_USAGE_CD": "RES",
            "STREET_LINE_1": "2200 Elm St",
            "CITY_NAME": "Dallas",
            "STATE_CD": "TX",
            "POSTAL_CD": "75001",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20250410",
        },
        {
            "CI_ADDR_ID": "HADDR-04",
            "CI_CUST_NUM": "HOG-554411",
            "ADDR_USAGE_CD": "RES",
            "STREET_LINE_1": "1725 Slough Ave",
            "CITY_NAME": "Scranton",
            "STATE_CD": "PA",
            "POSTAL_CD": "18503",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20250220",
        },
        {
            "CI_ADDR_ID": "HADDR-05",
            "CI_CUST_NUM": "HOG-991204",
            "ADDR_USAGE_CD": "RES",
            "STREET_LINE_1": "450 Hayes St",
            "CITY_NAME": "San Francisco",
            "STATE_CD": "CA",
            "POSTAL_CD": "94102",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20251112",
        },
        {
            "CI_ADDR_ID": "HADDR-06",
            "CI_CUST_NUM": "HOG-332190",
            "ADDR_USAGE_CD": "RES",
            "STREET_LINE_1": "110 Mulberry St",
            "CITY_NAME": "New York",
            "STATE_CD": "NY",
            "POSTAL_CD": "10013",
            "COUNTRY_CD": "US",
            "VERIFIED_DT": "20241005",
        },
    ]
    write_csv(brz_multi / "brz_hogan_ci_address.csv", hogan_ci_addresses)

    # 3. AFS Child Tables (1:N per BORROWER_ID)
    afs_facilities = [
        {
            "FACILITY_ID": "FAC-AFS-3001",
            "BORROWER_ID": "AFS-99120",
            "FACILITY_TYPE": "REVOLVER",
            "COMMITMENT_AMT": "250000.00",
            "OUTSTANDING_BAL": "115000.00",
            "RATE_INDEX": "SOFR_1M",
            "SPREAD_BPS": "225",
            "MATURITY_DATE": "2028-05-15",
            "FACILITY_STATUS": "ACTIVE",
        },
        {
            "FACILITY_ID": "FAC-AFS-3002",
            "BORROWER_ID": "AFS-99120",
            "FACILITY_TYPE": "EQUIPMENT_TERM",
            "COMMITMENT_AMT": "95000.00",
            "OUTSTANDING_BAL": "72400.00",
            "RATE_INDEX": "PRIME",
            "SPREAD_BPS": "150",
            "MATURITY_DATE": "2029-06-01",
            "FACILITY_STATUS": "ACTIVE",
        },
        {
            "FACILITY_ID": "FAC-AFS-3003",
            "BORROWER_ID": "AFS-11048",
            "FACILITY_TYPE": "REVOLVER",
            "COMMITMENT_AMT": "5000000.00",
            "OUTSTANDING_BAL": "3200000.00",
            "RATE_INDEX": "SOFR_3M",
            "SPREAD_BPS": "185",
            "MATURITY_DATE": "2030-06-01",
            "FACILITY_STATUS": "ACTIVE",
        },
        {
            "FACILITY_ID": "FAC-AFS-3004",
            "BORROWER_ID": "AFS-11048",
            "FACILITY_TYPE": "LETTER_OF_CREDIT",
            "COMMITMENT_AMT": "1200000.00",
            "OUTSTANDING_BAL": "1200000.00",
            "RATE_INDEX": "FIXED",
            "SPREAD_BPS": "125",
            "MATURITY_DATE": "2027-12-31",
            "FACILITY_STATUS": "ACTIVE",
        },
        {
            "FACILITY_ID": "FAC-AFS-3005",
            "BORROWER_ID": "AFS-44501",
            "FACILITY_TYPE": "TERM_LOAN_A",
            "COMMITMENT_AMT": "1500000.00",
            "OUTSTANDING_BAL": "1180000.00",
            "RATE_INDEX": "SOFR_1M",
            "SPREAD_BPS": "250",
            "MATURITY_DATE": "2029-04-10",
            "FACILITY_STATUS": "ACTIVE",
        },
        {
            "FACILITY_ID": "FAC-AFS-3006",
            "BORROWER_ID": "AFS-77812",
            "FACILITY_TYPE": "WORKING_CAPITAL_LINE",
            "COMMITMENT_AMT": "500000.00",
            "OUTSTANDING_BAL": "210000.00",
            "RATE_INDEX": "PRIME",
            "SPREAD_BPS": "200",
            "MATURITY_DATE": "2028-02-18",
            "FACILITY_STATUS": "ACTIVE",
        },
    ]
    write_csv(brz_multi / "brz_afs_facility.csv", afs_facilities)

    afs_collateral = [
        {
            "COLLATERAL_ID": "COL-801",
            "FACILITY_ID": "FAC-AFS-3001",
            "BORROWER_ID": "AFS-99120",
            "COLLATERAL_TYPE": "AR_RECEIVABLES",
            "APPRAISED_VALUE": "340000.00",
            "ADVANCE_RATE_PCT": "80.0",
            "VALUATION_DATE": "2026-05-01",
        },
        {
            "COLLATERAL_ID": "COL-802",
            "FACILITY_ID": "FAC-AFS-3003",
            "BORROWER_ID": "AFS-11048",
            "COLLATERAL_TYPE": "CRE_FIRST_LIEN",
            "APPRAISED_VALUE": "7500000.00",
            "ADVANCE_RATE_PCT": "75.0",
            "VALUATION_DATE": "2026-04-15",
        },
        {
            "COLLATERAL_ID": "COL-803",
            "FACILITY_ID": "FAC-AFS-3005",
            "BORROWER_ID": "AFS-44501",
            "COLLATERAL_TYPE": "EQUIPMENT_UCC",
            "APPRAISED_VALUE": "2100000.00",
            "ADVANCE_RATE_PCT": "70.0",
            "VALUATION_DATE": "2026-03-20",
        },
        {
            "COLLATERAL_ID": "COL-804",
            "FACILITY_ID": "FAC-AFS-3006",
            "BORROWER_ID": "AFS-77812",
            "COLLATERAL_TYPE": "AR_INVENTORY",
            "APPRAISED_VALUE": "680000.00",
            "ADVANCE_RATE_PCT": "75.0",
            "VALUATION_DATE": "2026-02-01",
        },
    ]
    write_csv(brz_multi / "brz_afs_collateral.csv", afs_collateral)

    afs_guarantors = [
        {
            "GUARANTOR_ID": "GUAR-501",
            "BORROWER_ID": "AFS-99120",
            "GUARANTOR_NAME": "Jane Margaret Doe",
            "GUARANTOR_SSN_TIN": "987-65-4321",
            "GUARANTOR_DOB": "1984-06-12",
            "GUARANTOR_PHONE": "312-555-0199",
            "GUARANTOR_EMAIL": "jane.doe@chicago-law.com",
            "GUARANTOR_ZIP": "60601",
            "GUARANTEE_PCT": "100.0",
        },
        {
            "GUARANTOR_ID": "GUAR-502",
            "BORROWER_ID": "AFS-44501",
            "GUARANTOR_NAME": "Robert Alan Smith",
            "GUARANTOR_SSN_TIN": "123-45-6789",
            "GUARANTOR_DOB": "1975-09-23",
            "GUARANTOR_PHONE": "214-555-0122",
            "GUARANTOR_EMAIL": "rsmith@lonestardrill.com",
            "GUARANTOR_ZIP": "75001",
            "GUARANTEE_PCT": "100.0",
        },
        {
            "GUARANTOR_ID": "GUAR-503",
            "BORROWER_ID": "AFS-77812",
            "GUARANTOR_NAME": "Michael Gary Scott",
            "GUARANTOR_SSN_TIN": "555-12-3456",
            "GUARANTOR_DOB": "1965-03-15",
            "GUARANTOR_PHONE": "570-555-0199",
            "GUARANTOR_EMAIL": "mscott@dundermifflin.com",
            "GUARANTOR_ZIP": "18503",
            "GUARANTEE_PCT": "100.0",
        },
        {
            "GUARANTOR_ID": "GUAR-504",
            "BORROWER_ID": "AFS-11048",
            "GUARANTOR_NAME": "Elena Rostov",
            "GUARANTOR_SSN_TIN": "",
            "GUARANTOR_DOB": "",
            "GUARANTOR_PHONE": "305-555-0199",
            "GUARANTOR_EMAIL": "e.rostov@apexlogistics.com",
            "GUARANTOR_ZIP": "33131",
            "GUARANTEE_PCT": "50.0",
        },
    ]
    write_csv(brz_multi / "brz_afs_guarantor.csv", afs_guarantors)

    # 4. TSYS Child Tables (1:N Card Accounts + Real-Time Kafka Auth Stream)
    tsys_card_accounts = [
        {
            "card_account_id": "CRD-ACCT-7001",
            "cardholder_id": "TSYS-88201",
            "product_code": "PLATINUM_REWARDS",
            "credit_limit": "15000.00",
            "current_balance": "3420.50",
            "available_credit": "11579.50",
            "apr_rate": "18.99",
            "days_past_due": "0",
            "account_status": "OPEN",
            "cycle_date": "2026-10-15",
        },
        {
            "card_account_id": "CRD-ACCT-7002",
            "cardholder_id": "TSYS-88201",
            "product_code": "CORP_EXECUTIVE",
            "credit_limit": "30000.00",
            "current_balance": "6150.00",
            "available_credit": "23850.00",
            "apr_rate": "16.49",
            "days_past_due": "0",
            "account_status": "OPEN",
            "cycle_date": "2026-10-15",
        },
        {
            "card_account_id": "CRD-ACCT-7003",
            "cardholder_id": "TSYS-44109",
            "product_code": "CASH_BACK_SIGNATURE",
            "credit_limit": "10000.00",
            "current_balance": "1890.25",
            "available_credit": "8109.75",
            "apr_rate": "19.99",
            "days_past_due": "0",
            "account_status": "OPEN",
            "cycle_date": "2026-10-01",
        },
        {
            "card_account_id": "CRD-ACCT-7004",
            "cardholder_id": "TSYS-66231",
            "product_code": "RETAIL_PREFERRED",
            "credit_limit": "8500.00",
            "current_balance": "2100.00",
            "available_credit": "6400.00",
            "apr_rate": "21.49",
            "days_past_due": "0",
            "account_status": "OPEN",
            "cycle_date": "2026-09-28",
        },
        {
            "card_account_id": "CRD-ACCT-7005",
            "cardholder_id": "TSYS-10049",
            "product_code": "SAPPHIRE_RESERVE",
            "credit_limit": "25000.00",
            "current_balance": "4780.00",
            "available_credit": "20220.00",
            "apr_rate": "17.99",
            "days_past_due": "0",
            "account_status": "OPEN",
            "cycle_date": "2026-10-20",
        },
        {
            "card_account_id": "CRD-ACCT-7006",
            "cardholder_id": "TSYS-99214",
            "product_code": "CLASSIC_RETAIL",
            "credit_limit": "5000.00",
            "current_balance": "4950.00",
            "available_credit": "50.00",
            "apr_rate": "24.99",
            "days_past_due": "35",
            "account_status": "RESTRICTED",
            "cycle_date": "2025-12-05",
        },
    ]
    write_csv(brz_multi / "brz_tsys_card_account.csv", tsys_card_accounts)

    tsys_auth_events = [
        {
            "auth_event_id": "AUTH-KFK-10001",
            "cardholder_id": "TSYS-88201",
            "card_account_id": "CRD-ACCT-7001",
            "event_ts": "2026-11-15T14:22:10Z",
            "merchant_name": "UNITED AIRLINES ORD",
            "mcc_code": "3000",
            "auth_amount": "642.80",
            "auth_status": "APPROVED",
            "verified_mobile": "+13125550199",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "0",
            "kafka_offset": "948201",
        },
        {
            "auth_event_id": "AUTH-KFK-10002",
            "cardholder_id": "TSYS-88201",
            "card_account_id": "CRD-ACCT-7002",
            "event_ts": "2026-11-16T09:15:44Z",
            "merchant_name": "WESTLAW LEGAL RESEARCH",
            "mcc_code": "7372",
            "auth_amount": "1250.00",
            "auth_status": "APPROVED",
            "verified_mobile": "+13125550199",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "0",
            "kafka_offset": "948202",
        },
        {
            "auth_event_id": "AUTH-KFK-10003",
            "cardholder_id": "TSYS-44109",
            "card_account_id": "CRD-ACCT-7003",
            "event_ts": "2026-11-16T11:05:19Z",
            "merchant_name": "SHELL OIL DALLAS",
            "mcc_code": "5541",
            "auth_amount": "78.45",
            "auth_status": "APPROVED",
            "verified_mobile": "+12145550122",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "1",
            "kafka_offset": "410290",
        },
        {
            "auth_event_id": "AUTH-KFK-10004",
            "cardholder_id": "TSYS-66231",
            "card_account_id": "CRD-ACCT-7004",
            "event_ts": "2026-11-16T16:40:02Z",
            "merchant_name": "HEB GROCERY HOUSTON",
            "mcc_code": "5411",
            "auth_amount": "154.20",
            "auth_status": "APPROVED",
            "verified_mobile": "+17135550144",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "1",
            "kafka_offset": "410291",
        },
        {
            "auth_event_id": "AUTH-KFK-10005",
            "cardholder_id": "TSYS-10049",
            "card_account_id": "CRD-ACCT-7005",
            "event_ts": "2026-11-17T08:12:33Z",
            "merchant_name": "FOUR SEASONS MIAMI",
            "mcc_code": "7011",
            "auth_amount": "890.00",
            "auth_status": "APPROVED",
            "verified_mobile": "+13055550133",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "2",
            "kafka_offset": "205110",
        },
        {
            "auth_event_id": "AUTH-KFK-10006",
            "cardholder_id": "TSYS-99214",
            "card_account_id": "CRD-ACCT-7006",
            "event_ts": "2026-11-17T19:55:01Z",
            "merchant_name": "GENCO OLIVE OIL IMPORTS",
            "mcc_code": "5499",
            "auth_amount": "2400.00",
            "auth_status": "DECLINED",
            "verified_mobile": "+12125550100",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "kafka_partition": "2",
            "kafka_offset": "205111",
        },
    ]
    write_csv(brz_multi / "brz_tsys_auth_events.csv", tsys_auth_events)
    write_jsonl(brz_multi / "brz_tsys_auth_events.jsonl", tsys_auth_events)

    # 5. ACLS Child Table (1:N Loan Applications / Contracts)
    acls_loan_apps = [
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5001",
            "APPLICANT_ID": "ACLS-44012",
            "PRODUCT_SUBTYPE": "AUTO_NEW",
            "PRINCIPAL_AMT": "35000.00",
            "CURRENT_BAL": "28400.00",
            "INTEREST_RATE": "5.49",
            "TERM_MONTHS": "60",
            "FICO_SCORE": "785",
            "CONTRACT_STATUS": "ACTIVE",
            "ORIGINATION_DATE": "2026-03-20",
        },
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5002",
            "APPLICANT_ID": "ACLS-44012",
            "PRODUCT_SUBTYPE": "HELOC_PRIME",
            "PRINCIPAL_AMT": "120000.00",
            "CURRENT_BAL": "45000.00",
            "INTEREST_RATE": "7.25",
            "TERM_MONTHS": "120",
            "FICO_SCORE": "785",
            "CONTRACT_STATUS": "ACTIVE",
            "ORIGINATION_DATE": "2026-08-01",
        },
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5003",
            "APPLICANT_ID": "ACLS-77192",
            "PRODUCT_SUBTYPE": "MORTGAGE_30YR_FIXED",
            "PRINCIPAL_AMT": "420000.00",
            "CURRENT_BAL": "411200.00",
            "INTEREST_RATE": "6.125",
            "TERM_MONTHS": "360",
            "FICO_SCORE": "760",
            "CONTRACT_STATUS": "ACTIVE",
            "ORIGINATION_DATE": "2026-05-11",
        },
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5004",
            "APPLICANT_ID": "ACLS-99882",
            "PRODUCT_SUBTYPE": "DEBT_CONSOLIDATION",
            "PRINCIPAL_AMT": "25000.00",
            "CURRENT_BAL": "21800.00",
            "INTEREST_RATE": "9.99",
            "TERM_MONTHS": "48",
            "FICO_SCORE": "710",
            "CONTRACT_STATUS": "ACTIVE",
            "ORIGINATION_DATE": "2026-04-05",
        },
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5005",
            "APPLICANT_ID": "ACLS-33104",
            "PRODUCT_SUBTYPE": "PERSONAL_UNSECURED",
            "PRINCIPAL_AMT": "15000.00",
            "CURRENT_BAL": "12900.00",
            "INTEREST_RATE": "10.49",
            "TERM_MONTHS": "36",
            "FICO_SCORE": "730",
            "CONTRACT_STATUS": "ACTIVE",
            "ORIGINATION_DATE": "2026-07-14",
        },
        {
            "LOAN_CONTRACT_ID": "LON-ACLS-5006",
            "APPLICANT_ID": "ACLS-11290",
            "PRODUCT_SUBTYPE": "COMMERCIAL_VEHICLE",
            "PRINCIPAL_AMT": "60000.00",
            "CURRENT_BAL": "0.00",
            "INTEREST_RATE": "6.75",
            "TERM_MONTHS": "48",
            "FICO_SCORE": "740",
            "CONTRACT_STATUS": "PAID_OFF",
            "ORIGINATION_DATE": "2024-01-15",
        },
    ]
    write_csv(brz_multi / "brz_acls_loan_application.csv", acls_loan_apps)

    # Mirror all banking_multi_table files into SILVER DATA PRODUCT/samples/banking_multi_table
    for fpath in brz_multi.iterdir():
        if fpath.is_file():
            shutil.copy2(fpath, slv_multi / fpath.name)

    # 6. Generate Water Account 360 multi-table samples (SAP ERP, 3D TRASAR IoT, Salesforce FSL)
    for target_root in (root / "BRONZE DATA PRODUCT", root / "SILVER DATA PRODUCT"):
        wdir = target_root / "samples" / "water_multi_table"
        wdir.mkdir(parents=True, exist_ok=True)
        write_csv(
            wdir / "brz_sap_water_customer.csv",
            [
                {
                    "KUNNR": "SAP-W-1001",
                    "NAME1": "MIDWEST_FOOD_BEVERAGE_CORP",
                    "STCD1": "36-4455667",
                    "STRAS": "500 Industrial Pkwy",
                    "ORT01": "Chicago",
                    "PSTLZ": "60601",
                    "TELF1": "312-555-0811",
                    "SMTP_ADDR": "ops@midwestfood.com",
                    "KTOKD": "CORP",
                    "STATUS": "ACTIVE",
                },
                {
                    "KUNNR": "SAP-W-1002",
                    "NAME1": "GULF_COAST_PETROCHEM_LLC",
                    "STCD1": "74-9988112",
                    "STRAS": "8800 Refinery Rd",
                    "ORT01": "Houston",
                    "PSTLZ": "77027",
                    "TELF1": "832-555-0922",
                    "SMTP_ADDR": "utilities@gulfpetro.com",
                    "KTOKD": "IND_PLANT",
                    "STATUS": "ACTIVE",
                },
            ],
        )
        write_csv(
            wdir / "brz_sap_service_contract.csv",
            [
                {
                    "VBELN": "CON-W-901",
                    "KUNNR": "SAP-W-1001",
                    "CONTRACT_TYPE": "3D_TRASAR_COOLING_CARE",
                    "ANNUAL_VALUE": "185000.00",
                    "PLANT_CODE": "PLT-CHI-01",
                    "STATUS": "ACTIVE",
                    "START_DATE": "2025-01-01",
                },
                {
                    "VBELN": "CON-W-902",
                    "KUNNR": "SAP-W-1002",
                    "CONTRACT_TYPE": "BOILER_MEMBRANE_TREATMENT",
                    "ANNUAL_VALUE": "420000.00",
                    "PLANT_CODE": "PLT-HOU-04",
                    "STATUS": "ACTIVE",
                    "START_DATE": "2024-06-15",
                },
            ],
        )
        write_csv(
            wdir / "brz_trasar_cooling_asset.csv",
            [
                {
                    "ASSET_SKID_ID": "TRASAR-SKID-01",
                    "CUSTOMER_TAX_ID": "364455667",
                    "FACILITY_NAME": "Midwest Food & Beverage Corp",
                    "ASSET_TYPE": "COOLING_TOWER",
                    "LOOP_CAPACITY_GAL": "250000",
                    "SITE_ZIP": "60601",
                    "PLANT_CONTACT_PHONE": "+13125550811",
                    "COMMISSION_DATE": "2025-01-15",
                },
                {
                    "ASSET_SKID_ID": "TRASAR-SKID-02",
                    "CUSTOMER_TAX_ID": "749988112",
                    "FACILITY_NAME": "Gulf Coast Petrochem LLC",
                    "ASSET_TYPE": "REVERSE_OSMOSIS",
                    "LOOP_CAPACITY_GAL": "800000",
                    "SITE_ZIP": "77027",
                    "PLANT_CONTACT_PHONE": "+18325550922",
                    "COMMISSION_DATE": "2024-07-01",
                },
            ],
        )
        write_csv(
            wdir / "brz_sfdc_plant_site.csv",
            [
                {
                    "SFDC_SITE_ID": "001W00001A",
                    "ACCOUNT_NAME": "Midwest Food & Beverage Corp",
                    "TAX_EIN": "36-4455667",
                    "SITE_EMAIL": "water.steward@midwestfood.com",
                    "SITE_PHONE": "(312) 555-0811",
                    "POSTAL_CODE": "60601",
                    "HYGIENE_TIER": "PLATINUM",
                },
                {
                    "SFDC_SITE_ID": "001W00002B",
                    "ACCOUNT_NAME": "Gulf Coast Petrochem LLC",
                    "TAX_EIN": "74-9988112",
                    "SITE_EMAIL": "ehs@gulfpetro.com",
                    "SITE_PHONE": "(832) 555-0922",
                    "POSTAL_CODE": "77027",
                    "HYGIENE_TIER": "ENTERPRISE",
                },
            ],
        )

    print(f"[OK] Generated {len(list(brz_multi.iterdir()))} files in {brz_multi}")


if __name__ == "__main__":
    generate_all()
