# Prereasoner - Excel Copilot

Excel task-pane add-in for the existing Prereasoner conversation APIs. This folder owns the Office XML manifest; its hosted task pane lives in `web/public/office/excel/` so it is served from the same HTTPS origin and Firebase project as the existing web app.

## Current state

The local implementation provides the Office ribbon/task-pane shell, bounded read-only workbook snapshots, Google/Microsoft sign-in dialogs, shared question/answer rendering, and a previous-conversations view. Microsoft sign-in requires a Microsoft Entra application and Firebase's Microsoft provider to be configured. Do not submit the current manifest to Microsoft Marketplace until account configuration and the supported host matrix are complete.

The manifest requests `ReadWriteDocument` because Microsoft requires that permission for Excel's application-specific JavaScript APIs, even when those APIs are used only to read. The current UI and implementation must not write to workbook cells or workbook metadata. It declares `ExcelApi` 1.2, the minimum needed for values-only used-range discovery.

## Development

The manifest task pane and authentication dialog are served from the existing HTTPS Prereasoner origin. Configure Firebase Auth's Microsoft provider with a Microsoft Entra app registration before enabling the Microsoft sign-in button; Google sign-in remains available for existing Prereasoner accounts and their conversation history. Run the pending database migrations and apply runtime grants before deploying the matching engine version.

Sideload `manifest.xml` in Excel's **Add-ins → More Add-ins → My Add-ins → Upload My Add-in** flow (the exact menu varies by Excel client). For local development, use Microsoft's Office Add-in debugging/sideload tooling with a trusted HTTPS dev server. The production manifest currently points to `chat.prereasoner.com`.

## Release checklist

- Create/configure the Microsoft identity application and enable the Firebase Microsoft provider; retain Google sign-in for existing Prereasoner accounts. These identities remain separate unless a user proves and links both; do not merge by matching email.
- Apply the new account-principal and Excel session database migrations, then apply least-privilege serving grants before deployment.
- Test Excel for Windows, Mac, and web at the supported API requirement set.
- Verify that a question never writes to the source workbook.
- Validate the HTTPS host framing policy, icon paths, and the Office task-pane/dialog auth flow.
- Complete the listing, privacy copy, publisher enrollment, and Microsoft certification review.
