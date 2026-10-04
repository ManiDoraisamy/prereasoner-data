# Agent Rules

Every working rule for AI agents in this repository is in [CLAUDE.md](CLAUDE.md). Read it before
changing anything and follow it; it is the single source of truth, and this file adds nothing to it.

At the owner's request, one rule is repeated here:

- NEVER add a privacy link, privacy notice, or any privacy text to the Google Sheets add-on sidebar
  or the Excel task pane. Google's OAuth consent screen and the Marketplace listing already link
  `/privacy`; an in-sidebar notice damaged the onboarding experience and was removed at the owner's
  order (2026-10-04). This holds for every review, release and redesign: the add-on surfaces show
  starters, the thread, the composer, the status of a sheet being read, and (once a chat exists) a
  header with New chat and the sheet's sync time, which the owner asked for (2026-10-04); nothing else.
