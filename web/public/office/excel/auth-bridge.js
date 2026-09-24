export function credentialForDialog(message, {OAuthProvider, GoogleAuthProvider}) {
  if (!message || !['google', 'microsoft'].includes(message.provider)) {
    throw new Error('The sign-in response was not recognized.');
  }
  const idToken = typeof message.idToken === 'string' && message.idToken ? message.idToken : undefined;
  const accessToken = typeof message.accessToken === 'string' && message.accessToken
    ? message.accessToken : undefined;
  if (!idToken && !accessToken) throw new Error('The identity provider returned no sign-in token.');
  if (message.provider === 'microsoft') {
    return new OAuthProvider('microsoft.com').credential({idToken, accessToken});
  }
  return GoogleAuthProvider.credential(idToken || null, accessToken || null);
}
