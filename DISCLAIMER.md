# CareerDesk Consolidated Notice

[简体中文](zh/DISCLAIMER.md)

This document consolidates CareerDesk's use, liability, privacy, security,
distribution, contribution, and third-party boundaries. It supplements, but does not
change, the [MIT License](LICENSE).

## Free open-source software and liability

CareerDesk is free open-source software shared by its maintainers. It is not a
commercial, recruitment, career-counselling, medical, psychological, or other
professional service. Downloading, installing, modifying, discussing, or using it
does not create a client, agency, employment, partnership, fiduciary, support, or
other service relationship with a maintainer.

CareerDesk is licensed under the [MIT License](LICENSE) and provided **as is**, without
express or implied warranties, including warranties of merchantability, fitness for a
particular purpose, non-infringement, correctness, availability, security,
compatibility, or continued maintenance. To the maximum extent permitted by
applicable law, authors and copyright holders are not liable for claims, losses,
damages, costs, data loss, third-party charges, inability to use the software, or
other liability arising from the software, its AI output, or related dealings.

## AI, employment, and wellbeing

Model-generated answers, scores, job information, research, résumé suggestions,
questions, and other output can be inaccurate, incomplete, outdated, or unsuitable.
Users must independently review important information and remain responsible for
their applications and decisions. CareerDesk does not guarantee an interview, offer,
employment outcome, or any other result.

Emotional-support language is general companionship during a job search. It is not
psychological counselling, diagnosis, treatment, crisis care, or a replacement for
professional help. Anyone in continuing distress or immediate danger should contact
a trusted person, qualified local service, or local emergency/crisis resource.

## No support or maintenance commitment

Maintainers may voluntarily discuss issues or review contributions, but do not
promise a response, fix, review, merge, release, security deadline, backward
compatibility, continued maintenance, service level, bounty, compensation, or
technical support. Tests, checksums, attestations, and security controls describe a
particular build or check; they are not a warranty that the software is safe or
defect-free.

GitHub Issues and Pull Requests are public community collaboration spaces, not a
customer-support channel. Do not post API keys, tokens, résumés, conversations,
private code, exploit details, or other personal/confidential material there.

## Local data and credentials

The desktop application has no maintainer-operated user account, central database,
sync service, telemetry service, or cloud CareerDesk backend. It binds to the local
loopback interface. Business data, résumés, uploads, configuration, and logs are kept
on the user's device, and maintainers cannot access them through CareerDesk.

Installed desktop builds store supported API keys in the operating system's native
credential store; source runs use the user's private local `.env`. The settings API
does not return raw keys to the frontend. The `.app` or `.exe` does not contain a
user's runtime data or system credentials, forwarding an installer does not forward
those items, and uninstalling the application does not automatically delete them.

Custom data-directory migration creates and verifies a backup and a new copy before
switching configuration; it does not automatically delete the old or restored copy.
Active SQLite data must not be placed on common sync or network drives. A `.jpbak`
contains business data, including conversation text, but not configuration, system
credentials, logs, or rebuildable search/vector indexes. Backups have integrity
checks but are not encrypted or signed. Users are responsible for device security,
credentials, independent backups, and suitable storage encryption.

## Network and third-party data flows

CareerDesk connects externally only when the relevant feature and configuration
allow it:

| Situation | Trigger and data sent | Recipient |
|---|---|---|
| Cloud LLM | The user configures a cloud model and starts an AI operation. Required prompts, conversation and relevant résumé, job, research, preference, question, rubric, evidence, or answer material may be sent. Résumé adaptation and interview generation can send full extractable résumé text and a full confirmed JD. | The selected model provider or compatible endpoint operator |
| Conversation embeddings | The user separately enables semantic enhancement and configures an OpenAI key. Chat fragments and retrieval queries are sent. Local full-text search remains available without it. | OpenAI `text-embedding-3-small` |
| Company research search | The user separately enables online research. Company name, role name, and search queries are sent; résumés and conversations are not included in search queries. | Configured Tavily, Brave, Google Programmable Search, self-hosted SearXNG, or optional unofficial DuckDuckGo fallback |
| Research page retrieval | Research follows a search result with a read-only GET. Public page content is used for the operation and is not stored as a page snapshot. | The website hosting the search result |
| Source installation/build | Package names, versions, and normal package-manager network metadata may be sent. | Python/npm registries or configured mirrors |
| External links | The user clicks a link. | The destination website |

Saving an API key does not independently authorize embeddings or online research.
Deep research and the unofficial DuckDuckGo fallback have separate controls. A
request already sent cannot be recalled. Third-party retention, training, processing,
pricing, quotas, security, availability, and disclosure are governed by the user's
agreement and settings with that provider. Maintainers do not collect API fees,
control those services, or accept responsibility for their acts or output.

Résumé text is not automatically stripped of names, email addresses, phone numbers,
schools, employers, or experience before a model operation because those facts may
be needed for the requested analysis. Long-material compression may require multiple
model calls. Host-side output cleaning does not mean the original input was
anonymized. The application discloses the relevant material near sensitive actions;
users should review that disclosure before continuing.

Application logs, errors, and metrics do not record résumé, JD, research-report, or
adaptation-report bodies. They may record task type, version, state, input/token
length, latency, failure type, and non-sensitive summaries. Real user résumés are not
automatically added to evaluation, training, or regression data.

`APP_STRICT_OFFLINE=true` pauses cloud models, remote embeddings, search, and page
retrieval inside the application and permits only loopback local model services.
Installing source dependencies happens before the application starts and is outside
that switch. Users who require offline operation from the first command must prepare
dependencies in advance or use a self-contained build.

The repository includes an optional server/Docker mode, but maintainers operate no
public instance. Anyone deploying a multi-user or public instance is independently
responsible for authentication, TLS, host security, access control, logs, backups,
credentials, user notices, and applicable privacy requirements. GitHub separately
processes repository visitors and contributors under GitHub's own policies.

## Unsigned convenience builds

GitHub Release macOS and Windows archives are convenience builds produced from the
tagged source by GitHub Actions without a maintainer or organization commercial code-
signing certificate. Archive filenames and build manifests identify them as
`UNSIGNED`; macOS uses ad-hoc signing for bundle integrity. Gatekeeper, SmartScreen,
or other security software may warn, block, or require additional confirmation.

After a macOS update, Keychain may ask whether the new build may access an existing
CareerDesk API-key item. Users should verify the requested item and should not enable
access for every application. Artifact attestations, SHA-256 checksums, tests, and
ad-hoc signatures help verify source and integrity; they do not prove that a build is
safe, defect-free, or fit for a particular purpose. Users decide whether to run a
build and should back up important data first.

## Security reports and testing

If the repository Security page offers **Report a vulnerability**, use that private
channel. Otherwise, open a minimal public Issue without exploit details, personal
data, credentials, or private code and ask for a private channel. A report does not
guarantee acknowledgement, a response, a fix, a release date, or public credit.

Test only devices, accounts, data, and services you own or are explicitly authorized
to test. Do not access other people's data, disrupt availability, publish exploitable
secrets, or upload real credentials or personal data. This notice grants no authority
to test model providers, GitHub, recruitment sites, or other third parties and is not
an additional legal safe harbor. Revoke and rotate a leaked key directly with its
provider rather than waiting for a maintainer.

## Contributions

Issues, documentation, patches, and continued development are welcome and voluntary.
Unless a Pull Request expressly states different terms that a maintainer accepts in
writing, submitting a contribution represents that the contributor has the right to
submit it, that it contains no unauthorized code, assets, personal data, credentials,
or trade secrets, and that the contributor licenses it under this repository's MIT
License. Submission does not guarantee review or acceptance.

## Third-party rights and notices

Third-party libraries, runtimes, build tools, assets, products, models, companies,
and services remain subject to their owners' rights and licenses. Mentioning or
supporting them does not imply sponsorship, endorsement, or partnership.

Self-contained builds include CareerDesk's MIT License and this notice. Required
license texts and attribution for bundled Python and npm packages remain separate in
the build's third-party notice inventory.
