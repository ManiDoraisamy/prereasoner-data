export function explainMicrosoftAuthError(error) {
  const code = String(error?.code || '');
  if (code.includes('operation-not-allowed')) {
    return 'Microsoft sign-in is not enabled for Prereasoner yet. Please contact support.';
  }
  if (code.includes('account-exists-with-different-credential')) {
    return 'This email is already used with a different Prereasoner sign-in. Accounts are not merged automatically. Contact support for help accessing saved conversations.';
  }
  if (code.includes('credential-already-in-use')) {
    return 'This Microsoft account is already connected to another Prereasoner account. Contact support for help.';
  }
  if (code.includes('popup-blocked')) {
    return 'The sign-in window was blocked. Allow pop-ups for chat.prereasoner.com, then try again.';
  }
  if (code.includes('unauthorized-domain')) {
    return 'This sign-in page is not authorized in Firebase Authentication yet. Please contact support.';
  }
  if (code.includes('invalid-credential')) {
    return 'Microsoft sign-in could not be completed. Check the Microsoft identity configuration and try again.';
  }
  if (code.includes('network-request-failed')) {
    return 'The network interrupted sign-in. Check your connection and try again.';
  }
  return 'Microsoft sign-in could not be completed. Please try again or contact support.';
}
