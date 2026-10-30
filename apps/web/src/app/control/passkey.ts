// Standard browser WebAuthn transport. Signature/UV/origin checked by the Host SDK.
function bytes(value: string): ArrayBuffer {
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, c => c.charCodeAt(0)).buffer;
}
function encoded(value: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export async function createPasskey(options: PublicKeyCredentialCreationOptions): Promise<object> {
  const credential = await navigator.credentials.create({ publicKey: {
    ...options, challenge: bytes(options.challenge as unknown as string),
    user: { ...options.user, id: bytes(options.user.id as unknown as string) },
    excludeCredentials: options.excludeCredentials?.map(c => ({ ...c, id: bytes(c.id as unknown as string) })),
  } }) as PublicKeyCredential | null;
  if (!credential) throw new Error("Passkey 未创建");
  const response = credential.response as AuthenticatorAttestationResponse;
  return { id: credential.id, rawId: encoded(credential.rawId), type: credential.type,
    response: { clientDataJSON: encoded(response.clientDataJSON), attestationObject: encoded(response.attestationObject), transports: response.getTransports() },
    clientExtensionResults: credential.getClientExtensionResults(), authenticatorAttachment: credential.authenticatorAttachment };
}
export async function authenticatePasskey(options: PublicKeyCredentialRequestOptions): Promise<object> {
  const credential = await navigator.credentials.get({ publicKey: {
    ...options, challenge: bytes(options.challenge as unknown as string),
    allowCredentials: options.allowCredentials?.map(c => ({ ...c, id: bytes(c.id as unknown as string) })),
  } }) as PublicKeyCredential | null;
  if (!credential) throw new Error("Passkey 登录取消");
  const response = credential.response as AuthenticatorAssertionResponse;
  return { id: credential.id, rawId: encoded(credential.rawId), type: credential.type,
    response: { clientDataJSON: encoded(response.clientDataJSON), authenticatorData: encoded(response.authenticatorData), signature: encoded(response.signature), userHandle: response.userHandle ? encoded(response.userHandle) : null },
    clientExtensionResults: credential.getClientExtensionResults(), authenticatorAttachment: credential.authenticatorAttachment };
}
