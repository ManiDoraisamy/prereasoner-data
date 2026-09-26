export function credentialForDialog(message, {OAuthProvider}) {
  if (!message || message.provider !== 'microsoft') {
    throw new Error('The sign-in response was not recognized.');
  }
  const idToken = typeof message.idToken === 'string' && message.idToken ? message.idToken : undefined;
  const accessToken = typeof message.accessToken === 'string' && message.accessToken
    ? message.accessToken : undefined;
  if (!idToken && !accessToken) throw new Error('The identity provider returned no sign-in token.');
  return new OAuthProvider('microsoft.com').credential({idToken, accessToken});
}
