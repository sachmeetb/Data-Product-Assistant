# Accenture Data Products Portal (Silver & Gold)

A unified enterprise portal that authenticates users via Accenture Corporate SSO and hosts direct, interactive access to the two Medallion Data Products deployed on Google Cloud Run:

1. **Silver Product (BFSI Silver Layer & Schema Library)**
   - Live URL: `https://proxy-bfsi-silver-frontend-v1-1-743146199.us-central1.run.app/`
   - Canonical conformed models (Party, Account, Payment, Transaction, Loan, etc.) aligned with ISO 20022, BIAN, and FIBO standards.
2. **Gold Product (Data Product Assistant v2)**
   - Live URL: `https://proxy-dp-assistant-v2-frontend-preview-743146199.us-central1.run.app`
   - Conversational AI agentic platform for discovering, designing, and synthesizing consumption-ready Gold fact/dimensional marts with automated STTM and Unity Catalog governance.

---

## Key Features

- **Accenture Brand Design System**: Full alignment with Accenture color palette (Accenture Purple `#A100FF`, dark `#7800C4`, accent orange `#FF6B00`, neutral surfaces).
- **Microsoft Entra ID / Corporate SSO Gate**: Mock single sign-on experience matching `Accenture Data Product Assistant 2.html` with animated credential verification and quick demo personas.
- **Interactive Dual Product Cards**:
  - Direct external link button (`Open in New Tab`) to open the product in a dedicated browser tab.
  - In-app workspace launcher (`Open in Portal`) to preview and interact with the application directly within the portal.
- **In-App Workspace Viewer**:
  - Live embedded view with quick product switcher (toggle between Silver and Gold instantly).
  - Sub-toolbar with reload, full screen tab launch, and breadcrumb back navigation to the selection hub.

---

## How to Run

### Quickest: Zero-Install Standalone Mode
Simply double-click or open `index.html` in any modern web browser:
```bash
start index.html
# Or open in Chrome/Edge directly
```

### Local HTTP Server Mode
To serve locally on port 3000 (Python or Node):
```bash
# Python
python -m http.server 3000

# Node / npx
npx serve .
```
Then navigate to `http://localhost:3000`.

---

## Architecture & Medallion Interoperability

```
┌─────────────────┐       ┌────────────────────────┐       ┌────────────────────────┐
│  Bronze Layer   │ ----> │     Silver Product     │ ----> │      Gold Product      │
│ (Raw Ingest)    │       │ (Conformed BFSI Models)│       │ (Data Product Assistant│
│                 │       │    Cloud Run v1.1      │       │     Cloud Run v2.0)    │
└─────────────────┘       └────────────────────────┘       └────────────────────────┘
```
