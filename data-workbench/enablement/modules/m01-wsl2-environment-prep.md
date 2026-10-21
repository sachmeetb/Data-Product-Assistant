# Module 1 — Windows → WSL2 Environment Prep

- **Type:** hands-on setup
- **Target length:** ~14 slides
- **Prereq:** Module 0 (orientation)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 1 of 11.** House style is in **Module 0** — follow that
> format for every slide (Beat → bullets → `> Concept callout` → `[SCREENSHOT PLACEHOLDER]`
> → `_Speaker notes:_`). The learner has completed Module 0 (orientation) and is now setting
> up the Linux environment DW requires; no prior WSL or Linux experience is assumed.
> This is a Windows-only setup lab — macOS and native Linux users skip it and go to Module 2.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

> **Author note.** No WSL2 doc exists in the repo — this whole module is authored
> fresh. Net-new claims are marked `[NET-NEW: …]` inline; the WSL/Docker mechanics
> are standard platform behaviour, the DW-specific reasons ("why WSL for *this*
> track") are the net-new part.

---

## Slide 1 — Get a WSL2 environment ready to run DW
**Beat:** frame the module — you're building the Linux box DW will run inside.

- You'll finish Module 1 with a **working WSL2 Ubuntu** on your Windows machine, **Docker
  reachable from inside it**, and the **DW code sitting on the Linux filesystem** —
  everything Module 2 needs to launch the stack.
- Audience assumption: you're **comfortable at a command line** but **new to WSL**.
  So we keep Linux basics light and spend the time on the **WSL2-specific** gotchas.
- This is a **setup lab, not theory** — every step is something you run and confirm.

_Speaker notes:_ Don't let anyone skip ahead to Module 2 on native Windows/PowerShell — the
launcher is a bash script (Slide 3). Twenty minutes here saves an hour of debugging there.

---

## Slide 2 — Objectives & starting point
**Beat:** what you can do at the end, and where you must be to start.

- **By the end you can:** (1) confirm a running Ubuntu with `wsl -l -v`; (2) run
  `docker info` successfully *from inside* WSL; (3) `ls` the DW repo from inside WSL,
  on the Linux filesystem.
- **Starting point:** a **Windows 10 (2004+) or Windows 11** machine where you can
  install software, plus the DW code (repo access **or** a handed-over zip).
- **Prereq:** Module 0 done — you know what DW is and the two-persona model.

> **Concept callout — why a Linux box at all.** DW's launcher, its agent skills, and
> its container topology are all **POSIX/Linux-first**. On Windows, WSL2 is how you get
> a real Linux userland without dual-booting or a full VM.

---

## Slide 3 — On Windows, WSL2 is the only supported path
**Beat:** set the hard rule up front so nobody fights the tooling.

- `[NET-NEW: track decision]` **There is no supported path outside WSL on Windows** for
  this track. Don't run the launcher from PowerShell or Git-Bash.
- The reason is concrete: **`./dwb` is a bash script** (`#!/usr/bin/env bash`, runs the
  system `python3`), the sample-loaders are shell scripts, and the tested container
  topology is **native Docker Engine running inside the WSL2 Ubuntu distro**.
- macOS and native Linux users skip WSL entirely and go straight to Module 2 — WSL is a
  **Windows-only** bridge.

> **Concept callout — local topology vs client deployment.** The local stack runs 4–6
> Docker containers: frontend, backend, Neo4j, and optionally sample databases and an
> embedded git server. For client deployments, the sample DB and embedded git containers
> would be replaced by the client's own systems and repos. Neo4j can be hosted two ways:
> **Neo4j Aura** (managed cloud, credentials provided) or **self-hosted via container** —
> both are configurable in Settings. The local topology you're setting up here is a teaching
> environment; it transfers directly to a team server or cloud deployment.

_Speaker notes:_ If someone insists on native Windows, that's an unsupported experiment,
not the track. Redirect them here.

---

## Slide 4 — What WSL2 is (the one-slide primer)
**Beat:** the mental model, no more than you need.

- **WSL = Windows Subsystem for Linux.** **WSL2** runs a **real Linux kernel inside a
  fast, lightweight VM** that Windows manages for you — not an emulator or a translation
  shim (that was WSL1).
- Practically: you get an **Ubuntu terminal** that behaves like a normal Linux box,
  shares your CPU/RAM, and integrates with Windows Explorer.
- **Why v2 specifically:** the Docker Engine and DW's containers need the real kernel +
  full system-call support that only **WSL2** provides (WSL1 can't run the Docker daemon).

> **Concept callout — one machine, two worlds.** After today you'll live in the **Ubuntu
> shell** for all DW work. Windows is still there (browser, editor) —
> the two sides share files and networking, which is exactly what the next slides set up.

---

## Slide 5 — Install & start WSL + Ubuntu
**Beat:** the actual install, one command.

- Open **PowerShell as Administrator** (this is the *only* time you use PowerShell) and run:
  ```powershell
  wsl --install
  ```
  This enables WSL2, installs the **Ubuntu** distro by default, and sets WSL2 as the
  default version. **Reboot if prompted.**
- On first launch Ubuntu asks you to **create a Linux username + password** — this is
  local to the distro, unrelated to your Windows login. Remember it (`sudo` needs it).
- Already had WSL installed? Make sure it's current: `wsl --update`, and
  `wsl --set-default-version 2`.

_Speaker notes:_ On older Windows 10 builds `wsl --install` may not exist — send them to
Windows Update first, or the manual "enable feature + install Ubuntu from the Store" path.

---

## Slide 6 — Confirm Ubuntu is running (v2)
**Beat:** the first checkpoint — don't move on until this is green.

- From PowerShell **or** the Ubuntu shell:
  ```powershell
  wsl -l -v
  ```
- You want to see **Ubuntu**, **STATE = Running** (or `Stopped` until you open it), and
  crucially **VERSION = 2**. A `VERSION 1` here is the classic trap — fix with
  `wsl --set-version Ubuntu 2`.
- Launch the distro any time by typing `wsl` (or picking **Ubuntu** from the Start menu /
  Windows Terminal dropdown).

[SCREENSHOT PLACEHOLDER — SIZE: Small]
Name: `wsl-l-v`
Caption: `wsl -l -v` showing Ubuntu on VERSION 2, Running.
Must show: The terminal output with the `NAME`/`STATE`/`VERSION` columns, an `Ubuntu`
row, `Running`, and `2` clearly visible.
Taken at: After first launch of Ubuntu, run `wsl -l -v`.

---

## Slide 7 — Install Docker Engine in Ubuntu
**Beat:** install Docker directly inside the distro so `docker` works in the shell.

- **Install** via Docker's convenience script from the **Ubuntu shell**:
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```
  This installs the **engine**, the `docker` **CLI**, **`containerd`**, and the
  **Compose** + **Buildx** plugins. (Officially supported for dev/test; for the manual
  apt-repo path see `docs.docker.com/engine/install/ubuntu`.)
- `[NET-NEW: track guidance]` **Post-install — run without `sudo`.** Add yourself to the
  `docker` group so you don't prefix every command with `sudo`:
  ```bash
  sudo usermod -aG docker $USER && newgrp docker   # or reopen the Ubuntu shell
  ```
- `[NET-NEW: track guidance]` **Post-install — start the daemon each session.** WSL
  doesn't run systemd by default, so the Docker daemon isn't started automatically.
  Run this **every time you open Ubuntu** before using DW:
  ```bash
  sudo service docker start
  ```
  If `docker` reports **"Cannot connect to the Docker daemon"**, this is the fix.

_Speaker notes:_ **Docker Desktop vs Docker Engine CE — the Accenture-specific gotcha.**
Docker Desktop requires a commercial license for organizations above 250 employees and may be
blocked by enterprise extension policy. Docker Engine CE (what this slide installs) does **not**
require a commercial license. WSL runs a real Ubuntu VM via the Windows Hypervisor — Docker
inside it is the same as Docker on any remote or cloud Linux host. Frame this as "why we use
the CLI approach" to pre-empt the licensing question rather than wait for it to become a
two-day blocker.

[SCREENSHOT PLACEHOLDER — SIZE: Small]
Name: `docker-hello-world`
Caption: `docker run hello-world` succeeding inside the Ubuntu shell.
Must show: The Ubuntu prompt, the `docker run hello-world` command, and the output
including the **"Hello from Docker!"** line.
Taken at: In the Ubuntu shell, after install and `sudo service docker start`.

---

## Slide 8 — `host.docker.internal` on WSL
**Beat:** the one networking name you'll need — what it means here.

- A container **cannot reach services on your machine via `localhost`** — inside a
  container, `localhost` is the container itself.
- The fix is the special hostname **`host.docker.internal`**, which resolves to your
  **host machine's gateway**. DW's `docker-compose.yml` **already wires it** via
  `extra_hosts` (`host-gateway`), so it resolves to your **WSL2 Ubuntu host** — the
  machine a DB you run yourself would live on.
- You mostly won't touch it in Module 2 (the sample DBs run **as containers**, reachable by
  service name). It matters later — **Module 9, bring-your-own-data** — when you point a project
  at a database you're running yourself.

> **Concept callout — remember this for Module 9, not now.** Flag the name today so it isn't a
> surprise later: "container can't see my DB on localhost → use `host.docker.internal`."

---

## Slide 9 — Get the code in: two paths
**Beat:** how the DW repo lands inside Ubuntu — pick the one you were given.

- **Path A — read-only repo clone (preferred if you have access):** from the **Ubuntu
  shell**, `git clone <repo-url>` into your Linux home (e.g. `~/working/`). Clone
  read-only; you won't be pushing back to the source of truth from here.
- **Path B — zip transfer (if you were handed a zip):** copy the zip into WSL and unzip
  it **on the Linux side**. Easiest reliable move: from Ubuntu, `cp
  /mnt/c/Users/<you>/Downloads/data-workbench.zip ~/working/ && cd ~/working && unzip
  data-workbench.zip`.
- **Either way, the repo must end up under your Linux home** (`~/…`), **not** left under
  `/mnt/c/...` — the next slide explains why that matters a lot.

_Speaker notes:_ `/mnt/c/...` is your Windows `C:` drive seen from Ubuntu — fine as a
*conduit* to copy a zip across, wrong as a *home* for the repo.

---

## Slide 10 — WSL filesystem mechanics (the perf gotcha)
**Beat:** the single biggest "why is this so slow" mistake — avoid it.

- WSL gives you **two filesystems**: the fast **Linux fs** (your `~/` home, lives inside
  the WSL2 VM) and the **Windows fs** mounted at **`/mnt/c`, `/mnt/d`, …**.
- **Cross-filesystem I/O is slow.** Running Docker builds, `git`, or the DW stack against
  a repo on `/mnt/c` can be **many times slower** than the Linux fs. **Keep the repo on
  the Linux fs** (`~/working/data-workbench`).
- Need to browse those Linux files from Windows? Open Explorer at **`\\wsl$`** (or
  `\\wsl.localhost\Ubuntu`) — your distro shows up as a network location you can read and
  edit with Windows tools (e.g. VS Code via **Remote - WSL**).

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `wsl-explorer-path`
Caption: Browsing the Linux fs from Windows Explorer via `\\wsl$`.
Must show: A Windows Explorer window with the address bar reading `\\wsl$\Ubuntu` (or
`\\wsl.localhost\Ubuntu`) and the DW repo folder visible under the Linux home.
Taken at: After the repo is cloned/unzipped on the Linux fs; open Explorer to `\\wsl$`.

---

## Slide 11 — Practical smooth-running tips
**Beat:** the small habits that keep WSL out of your way.

- **Live in the Ubuntu shell.** Do all DW work (`./dwb …`, `git`, editing) from inside
  Ubuntu, on the Linux fs. Treat Windows as the browser (and your editor's front end).
- **Use Windows Terminal** with an Ubuntu tab — much nicer than the default console.
- **Editor:** VS Code with **Remote - WSL** edits Linux-fs files natively (type `code .`
  in the repo from Ubuntu).
- `[NET-NEW: sizing preview]` Give WSL2 enough RAM — the DW stack is heavy (full sizing
  is Module 2's first topic). If needed, cap/raise WSL memory via a **`.wslconfig`** in your
  Windows home. `wsl --shutdown` (from PowerShell) is the clean "turn it off and on
  again" for a wedged distro.

---

## Slide 12 — Outcome — what "done" looks like
**Beat:** the concrete end-state to check yourself against.

- `wsl -l -v` shows **Ubuntu, VERSION 2, Running**.
- **Docker Engine** is installed in Ubuntu and the daemon is started (`sudo service
  docker start`; `docker info` succeeds).
- The **DW repo is on the Linux fs** (e.g. `~/working/data-workbench`) — `docker-compose.yml`
  and the `dwb` launcher are visible at its top level.
- You have **not** left the repo under `/mnt/c`.

_Speaker notes:_ This is the "start line" for Module 2. If any of these four is off, fix it now.

---

## Slide 13 — Validation (run these two checks)
**Beat:** prove it works — the green check you run yourself.

- **Check 1 — Docker reachable from inside WSL.** From the **Ubuntu shell**, start the
  daemon first (`sudo service docker start`), then:
  ```bash
  docker info
  ```
  It should print server/engine details with **no error** — that confirms the Docker
  Engine is running inside this distro.
- **Check 2 — repo present on the Linux fs.** From the repo dir in Ubuntu:
  ```bash
  cd ~/working/data-workbench && ls
  ```
  You should see `docker-compose.yml`, `dwb`, `workbench/`, `workbench-skills/`, `cli/`.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `docker-info-in-wsl`
Caption: `docker info` succeeding inside the Ubuntu shell.
Must show: The Ubuntu prompt, the `docker info` command, and output including a **Server**
section (no "Cannot connect to the Docker daemon" error).
Taken at: In the Ubuntu shell, after installing Docker Engine and starting the daemon.

---

## Slide 14 — Next steps
**Beat:** close Module 1; point at the launch module.

- You now have a **Linux box** (WSL2 Ubuntu) with **Docker** and the **DW code** on the
  fast filesystem — the exact prerequisites Module 2 assumes.
- **Self-check before moving on:**
  1. Why must the repo live on the Linux fs and not `/mnt/c`?
  2. What does `host.docker.internal` do, and which later module needs it?
  3. How do you start the Docker daemon in WSL each session (and what error tells you
     you forgot)?
- **Next:** **Module 2 — Install, Configure & Launch Locally** — size the machine, provide the
  LLM key, and bring the stack up with `./dwb up --with postgres`.
