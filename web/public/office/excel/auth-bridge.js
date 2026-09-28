export function credentialForDialog(message, {OAuthProvider}) {
  if (!message || message.provider !== 'microsoft') {
    throw new Error('The sign-in response was not recognized.');
  }
  if (message.credential && typeof message.credential === 'object') {
    const credential = OAuthProvider.credentialFromJSON(message.credential);
    if (credential.providerId !== 'microsoft.com') {
      throw new Error('The sign-in response used an unexpected identity provider.');
    }
    return credential;
  }
  // Accept the earlier payload format during a rolling update, while new dialogs preserve
  // all credential fields with Firebase's JSON serializer above.
  const idToken = typeof message.idToken === 'string' && message.idToken ? message.idToken : undefined;
  const accessToken = typeof message.accessToken === 'string' && message.accessToken
    ? message.accessToken : undefined;
  if (!idToken && !accessToken) throw new Error('The identity provider returned no sign-in token.');
  return new OAuthProvider('microsoft.com').credential({idToken, accessToken});
}
