<p align="center">
  <a href="../README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <img src="../frontend/public/logo-light-512.png" alt="CareerDesk logo" width="112" />
</p>

<h1 align="center">CareerDesk: Your Personal Career Assistant</h1>

<p align="center">
  <strong>An AI-agent-assisted workspace for managing your job applications, completely free and open source.</strong><br />
  I built this for my own job hunt this year, and open-sourced it hoping it helps you with yours.<br />
  May we all get off to a winning start this recruiting season.
</p>

<p align="center">
  <a href="../backend/pyproject.toml"><img alt="Python 3.12–3.13" src="https://img.shields.io/badge/Python-3.12%E2%80%933.13-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white" /></a>
  <a href="../backend/"><img alt="Backend FastAPI" src="https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&amp;logo=fastapi&amp;logoColor=white" /></a>
  <a href="../frontend/"><img alt="Frontend React 19" src="https://img.shields.io/badge/Frontend-React%2019-61DAFB?style=for-the-badge&amp;logo=react&amp;logoColor=20232A" /></a>
  <br />
  <a href="../backend/pyproject.toml"><img alt="Version 1.0.1" src="https://img.shields.io/badge/Version-v1.0.1-EA6B38?style=for-the-badge" /></a>
  <a href="https://github.com/xinhuangcs/CareerDesk/actions/workflows/unsigned-release.yml"><img alt="Build GitHub Actions" src="https://img.shields.io/badge/Build-GitHub%20Actions-6E78FF?style=for-the-badge&amp;logo=githubactions&amp;logoColor=white" /></a>
  <a href="../LICENSE"><img alt="License MIT" src="https://img.shields.io/badge/License-MIT-E1B800?style=for-the-badge" /></a>
</p>

<p align="center">
  <a href="#-what-is-careerdesk">What Is CareerDesk?</a> ·
  <a href="#-key-features">Key Features</a> ·
  <a href="#-install-and-get-started">Quick Start</a> ·
  <a href="#-privacy-and-security">Privacy &amp; Security</a> ·
  <a href="#-contributing">Contributing</a>
</p>

---

## 🧭 What Is CareerDesk?

CareerDesk is a completely free and open-source workspace for managing your job applications. It combines visual application tracking with a personal application assistant that answers questions about your recruiting season from your own records, including application analysis, weekly planning, interview reflection, and emotional support. A set of built-in deterministic workflows handles company and role research, résumé fit analysis, question-set generation, and mock interviews.

> CareerDesk is a local desktop application, not a hosted web service or an automated job-application tool.

https://github.com/user-attachments/assets/5230d010-0f2d-493e-b088-3bbbd7969572

<p align="center"><sub>A short walkthrough of the board, research, and the assistant.</sub></p>

## ✨ Key Features

<table>
  <tr>
    <td width="100%" valign="top">
      <h3>📊 Job Application Progress Board</h3>
      <p>Still managing applications across spreadsheets, notes, and multiple job platforms? Put every company, role, and stage on one visual board—and import your existing files in one go.</p>
    </td>
  </tr>
  <tr>
    <td width="100%" valign="top">
      <h3>🔎 Company and Role Research</h3>
      <p>Tired of researching companies one by one? Complete company and role research with one click, then get a clear report with source citations.</p>
    </td>
  </tr>
  <tr>
    <td width="100%" valign="top">
      <h3>📝 Résumé Fit Analysis</h3>
      <p>Wonder whether your résumé really fits the role? Compare it with the full job description to uncover strengths, gaps, and useful edits.</p>
    </td>
  </tr>
  <tr>
    <td width="100%" valign="top">
      <h3>🎯 Role-Specific Practice</h3>
      <p>Want to know what the interview might ask? Generate questions from your résumé and JD, practise at your own pace, and receive structured feedback.</p>
    </td>
  </tr>
  <tr>
    <td width="100%" valign="top">
      <h3>🤝 Job Application Assistant</h3>
      <p>A recruiting season can feel exhausting when you face it alone. Let the assistant organize roles, analyse applications, make plans, or simply keep you company for a chat.</p>
    </td>
  </tr>
  <tr>
    <td width="100%" valign="top">
      <h3>🧠 Your Choice of Model</h3>
      <p>Connect mainstream cloud models or local Ollama, vLLM, and SGLang. Even without a model, the application board remains available.</p>
    </td>
  </tr>
</table>

## 🚀 Install and Get Started

Download the appropriate convenience build from [GitHub Releases](https://github.com/xinhuangcs/CareerDesk/releases):

- **macOS Apple Silicon:** `CareerDesk-<version>-macos-arm64-UNSIGNED.zip`
- **Windows x64:** `CareerDesk-<version>-windows-x64-UNSIGNED.zip`

### Allow it through on first launch

This project does not buy Apple or Microsoft code-signing certificates, so both systems treat the app as coming from an unidentified developer. **You are seeing this because the build is unsigned, not because your system detected anything harmful.** Every build is produced by GitHub Actions from public source, and each release ships SHA-256 checksums and a build attestation you can verify yourself.

**macOS**

1. Unzip and move `CareerDesk.app` into your Applications folder (or anywhere you like — your data never moves with the app)
2. Double-click it. macOS says the developer cannot be verified — click **Done**
3. Open **System Settings → Privacy & Security**, scroll to the **Security** section
4. Find "CareerDesk was blocked" and click **Open Anyway**
5. Click **Open Anyway** once more and authenticate

**Windows**

1. **Extract the whole archive first** (any location works) — do not run it from inside the zip
2. Run `CareerDesk.exe` inside the `CareerDesk` folder
3. When "Windows protected your PC" appears, click **More info**
4. Click **Run anyway**

Both are one-time steps.

**Windows-only notes** (macOS users need none of this)

- `careerdesk-data(.exe)` in the folder is the backup/restore command-line tool; double-clicking it shows usage. Open `CareerDesk` for everyday use.
- The browser mode is fully functional; if you would rather have a standalone app window, install the Microsoft Edge WebView2 Runtime and reopen (the browser is used automatically when that component is missing).
- For a desktop shortcut, double-click `Add-Desktop-Shortcut.cmd` inside the folder once; the icon it creates can be moved anywhere. Re-run it whenever you move the folder itself.

## 🔐 Privacy and Security

### Privacy

Your applications, résumés, interview notes, conversations, and generated artifacts stay on your computer. Only when you explicitly use an authorized external service is the material required for that operation sent to the LLM or other provider you configured. Strict offline mode can pause all in-app network capabilities.

### Security

The codebase has gone through multiple AI-assisted review rounds using Fable 5 and GPT-5.6-sol. Tagged builds come from repository source through GitHub Actions, with release checks, frozen-app smoke tests, SHA-256 checksums, and build attestation. This makes packaging traceable but is not an independent security audit or a guarantee.

## 🤝 Contributing

Every Issue and PR is welcome. Add a theme or background, propose a feature, fix a bug, help test another platform, or simply tell us what you need. Source setup, the architecture diagram, local checks, and the PR workflow are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## 📄 License, Attribution, and Disclaimer

CareerDesk is released under the [MIT License](../LICENSE). Third-party libraries, product names, model names, company names, and other referenced materials remain the property of their respective owners; their inclusion does not imply affiliation or endorsement. Required dependency licenses and attribution remain bundled separately with desktop distributions.

This project was developed with assistance from Fable 5 and GPT-5.6-sol; AI-assisted development can still produce mistakes. CareerDesk and its AI output are provided as-is. See the [consolidated notice](../DISCLAIMER.md) for liability, privacy, network access, unsigned builds, security reporting, contributions, and third-party rights.
