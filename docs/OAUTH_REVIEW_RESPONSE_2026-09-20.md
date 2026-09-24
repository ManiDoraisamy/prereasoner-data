# OAuth verification response — project 271377281957

The replacement unlisted demonstration video is available at https://youtu.be/hXyQ9CYCfBM.

## Email reply

Hello Third-Party Data Safety Team,

Thank you for the review. We completed the requested remediation for project `271377281957`
(`prereasoner-inference`).

1. **Privacy policy and data protection.** We updated the existing privacy policy at
   https://chat.prereasoner.com/privacy. The new **Data protection** section describes HTTPS/TLS
   encryption in transit, Google Cloud-managed encryption at rest, server-verified Firebase
   identity, per-user ownership enforcement, Firebase access rules, IAM-authenticated Cloud SQL
   access, least-privilege service accounts and database roles, Secret Manager, data-minimized logs,
   bounded requests and storage, and limits on human access.
2. **Google API Limited Use and AI/ML.** The privacy policy now affirmatively states that
   Prereasoner's use of information received from Google Workspace APIs adheres to the Google User
   Data and Developer Policy, including its Limited Use requirements. It also states that raw,
   aggregated, anonymized, or derived Google user data is not used, transferred, or sold to create,
   train, or improve generalized or non-personalized AI/ML models. The add-on now displays a concise
   version of this disclosure before a user submits a question.
3. **Replacement demonstration video.** https://youtu.be/hXyQ9CYCfBM
4. **Scope matching.** Apps Script version `20` and the Google Cloud OAuth Data Access
   configuration request the same six scopes:
   - `openid`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `https://www.googleapis.com/auth/userinfo.profile`
   - `https://www.googleapis.com/auth/script.external_request`
   - `https://www.googleapis.com/auth/script.container.ui`
   - `https://www.googleapis.com/auth/spreadsheets.currentonly`

The application does not request a Google file-storage scope and does not write to or delete source
spreadsheet data.

## Reviewer navigation

No separate Prereasoner username, password, phone verification, payment card, or preconfigured test
account is required. The reviewer can use their Google test account:

1. Open a spreadsheet containing a header row and at least one data row.
2. Choose **Extensions → Prereasoner → Ask a question**.
3. Complete the Google consent flow. Expand **Show all services** to review every requested scope.
4. In the sidebar, ask a question such as `What is the total amount?`.
5. Review the returned answer and expand **Reasoning steps**.
6. Ask a follow-up question in the same sidebar.
7. Choose **Extensions → Prereasoner → Previous conversations** and open the saved conversation.

The two Apps Script scopes called out in the review are necessary as follows:

- `script.container.ui` creates the **Prereasoner** Extensions menu, the question-and-answer sidebar,
  and the Previous conversations dialog inside Google Sheets. No narrower Apps Script scope provides
  those container UI capabilities.
- `script.external_request` lets server-side Apps Script call the allowlisted Prereasoner and Firebase
  endpoints to authenticate the user, send the bounded current-spreadsheet snapshot and question,
  receive the answer and reasoning, save sidebar state, and retrieve the user's previous
  conversations. Apps Script's `UrlFetchApp` requires this scope; it has no narrower per-domain OAuth
  scope. The manifest additionally restricts destinations with `urlFetchWhitelist`.

Please continue the verification review. We are available to provide any further clarification.

Best regards,

Mani Doraisamy  
MailRecipe LLC
