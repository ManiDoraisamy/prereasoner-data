# Prereasoner - Excel Copilot

Excel task-pane add-in for the existing Prereasoner conversation APIs. This folder owns the Office XML manifest; its hosted task pane lives in `web/public/office/excel/` so it is served from the same HTTPS origin and Firebase project as the existing web app.

## Current state

The Office ribbon/task-pane shell, bounded read-only workbook snapshots, Microsoft sign-in flow, shared question/answer rendering, and previous-conversations view are implemented. Firebase Hosting is deployed and Microsoft's production manifest validator passes. Microsoft sign-in is not configured: Firebase has no Entra application ID or secret. Do not submit until sign-in is configured, the end-to-end question flow is tested, and the supported host matrix is complete. Listing copy is in `MARKETPLACE_LISTING.md`.

The manifest requests `ReadWriteDocument` because Microsoft requires that permission for Excel's application-specific JavaScript APIs, even when those APIs are used only to read. The current UI and implementation must not write to workbook cells or workbook metadata. It declares `ExcelApi` 1.2, the minimum needed for values-only used-range discovery.

## Development

The manifest task pane and authentication dialog are served from the existing HTTPS Prereasoner origin. Configure Firebase Authentication's Microsoft provider with a Microsoft Entra app registration before enabling sign-in. Microsoft and Google sign-ins are separate identities; matching email addresses do not merge accounts or conversation history. Confirm production database migrations and runtime grants before rolling out the matching engine version.

Sideload `manifest.xml` in Excel's **Add-ins → More Add-ins → My Add-ins → Upload My Add-in** flow (the exact menu varies by Excel client). For local development, use Microsoft's Office Add-in debugging/sideload tooling with a trusted HTTPS dev server. The production manifest currently points to `chat.prereasoner.com`.

## Release checklist

- Create/configure the Microsoft identity application and enable the Firebase Microsoft provider. Do not merge Google and Microsoft accounts by matching email.
- Confirm the account-principal and Excel session database migrations, then verify least-privilege serving grants before rollout.
- Test Excel for Windows, Mac, and web at the supported API requirement set. Current live verification reached the Excel web task pane and workbook reader, but the question flow remains unverified because the Prereasoner authentication provider is not configured for the signed-in Microsoft account.
- Verify that a question never writes to the source workbook.
- Validate the HTTPS host framing policy, icon paths, and the Office task-pane/dialog auth flow.
- Complete the listing, privacy copy, publisher enrollment, and Microsoft certification review.
