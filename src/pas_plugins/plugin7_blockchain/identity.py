"""Digital identity: W3C-style DIDs and verifiable credentials.

The point of putting an identity layer under beneficiaries is narrow and
practical. A beneficiary should not have to hand a carrier their passport to be
named on a policy, and a carrier should be able to check that somebody really is
who a credential says they are without holding that person's documents.

What is implemented:

* **`did:key`** - Ed25519 public keys as DIDs. Chosen because it needs no registry
  and no rotation infrastructure, which is the right default for a reference
  implementation; a `did:web` or Fabric-MSP resolver slots in behind `DIDResolver`.
* **DID Documents** with real Ed25519 verification methods.
* **Verifiable Credentials** whose proof is a genuine Ed25519 signature over the
  canonical unsigned payload, so verification is real cryptography rather than a
  flag in the JSON.

Verification checks three things, and reports which failed: the signature, the
issuer's key, and whether the credential has been revoked or expired.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pas_plugins.plugin7_blockchain.ledger import SigningKeyStore, canonical
from pas_plugins.plugin7_blockchain.models import (
    DidDocument,
    ProofType,
    VerifiableCredential,
    VerifiableCredentialStatus,
)


class CredentialRevocationRegistry:
    """Revocation by identifier.

    A public ledger cannot delete a credential without deleting the fact that it
    ever existed, so revocation is itself an append-only fact. This mirrors that:
    a revoked credential keeps its issuance record and gains a revocation entry.
    """

    def __init__(self) -> None:
        self._revoked: dict[str, dict[str, Any]] = {}

    def revoke(self, credential_id: str, reason: str, revoked_by: str) -> dict[str, Any]:
        if credential_id in self._revoked:
            msg = f"credential '{credential_id}' is already revoked"
            raise ValueError(msg)
        entry = {
            "credentialId": credential_id,
            "reason": reason,
            "revokedBy": revoked_by,
            "revokedAt": datetime.now(UTC).isoformat(),
        }
        self._revoked[credential_id] = entry
        return entry

    def is_revoked(self, credential_id: str) -> bool:
        return credential_id in self._revoked

    def entry(self, credential_id: str) -> dict[str, Any] | None:
        return self._revoked.get(credential_id)

    @property
    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._revoked)


class DIDResolver(Protocol):
    """Resolves a DID to its document. `did:key` is in-process; others are adapters."""

    def resolve(self, did: str) -> DidDocument | None: ...


class DidKeyResolver:
    """Resolves `did:key` identifiers from an in-process keystore."""

    def __init__(self, keystore: SigningKeyStore) -> None:
        self._keystore = keystore
        self._documents: dict[str, DidDocument] = {}

    def register(self, subject: str) -> DidDocument:
        """Create a DID and document for a subject."""
        public_key = self._keystore.public_key(subject)
        if public_key is None:
            msg = f"subject '{subject}' has no key pair; generate one first"
            raise KeyError(msg)
        did = self.did_for_key(public_key)
        key_id = f"{did}#key-1"
        document = DidDocument(
            id=did,
            controller=did,
            verification_method=[
                {
                    "id": key_id,
                    "type": "Ed25519VerificationKey2020",
                    "controller": did,
                    "publicKeyMultibase": public_key,
                }
            ],
            authentication=[key_id],
            assertion_method=[key_id],
        )
        self._documents[did] = document
        return document

    def resolve(self, did: str) -> DidDocument | None:
        return self._documents.get(did)

    def resolve_by_subject(self, subject: str) -> DidDocument | None:
        """The DID document for a local subject, or None if it has not registered."""
        public_key = self._keystore.public_key(subject)
        if public_key is None:
            return None
        return self._documents.get(self.did_for_key(public_key))

    def subject_for(self, did: str) -> str | None:
        for subject in self._keystore.subjects():
            if self.did_for_key(self._keystore.public_key(subject) or "") == did:
                return subject
        return None

    @staticmethod
    def did_for_key(public_key_hex: str) -> str:
        """`did:key` encodes the multibase key directly in the identifier."""
        if not public_key_hex:
            msg = "cannot derive a DID from an empty key"
            raise ValueError(msg)
        return f"did:key:z{public_key_hex[:32]}"


class IdentityService:
    """Issues, resolves and verifies verifiable credentials."""

    def __init__(self, keystore: SigningKeyStore | None = None) -> None:
        self.keystore = keystore or SigningKeyStore()
        self.resolver = DidKeyResolver(self.keystore)
        self.revocations = CredentialRevocationRegistry()
        self.credentials: dict[str, VerifiableCredential] = {}

    # -- DIDs --------------------------------------------------------------

    def create_did(self, subject: str) -> DidDocument:
        """Generate a key for a subject if needed, and return its DID document."""
        if self.keystore.public_key(subject) is None:
            self.keystore.generate(subject)
        return self.resolver.register(subject)

    def did_for(self, subject: str) -> str | None:
        document = self.resolver.resolve_by_subject(subject)
        return document.id if document else None

    def resolve(self, did: str) -> DidDocument | None:
        return self.resolver.resolve(did)

    # -- credentials -------------------------------------------------------

    def issue_credential(
        self,
        *,
        issuer_subject: str,
        subject: dict[str, Any],
        credential_type: str = "BeneficiaryRelationship",
        expires_in_days: int | None = 365,
        credential_id: str | None = None,
    ) -> VerifiableCredential:
        """Issue and sign a verifiable credential."""
        issuer_did = self.did_for(issuer_subject)
        if issuer_did is None:
            self.create_did(issuer_subject)
            issuer_did = self.did_for(issuer_subject)

        credential = VerifiableCredential(
            id=credential_id or f"urn:uuid:{hashlib.sha256(canonical(subject)).hexdigest()[:32]}",
            type=["VerifiableCredential", credential_type],
            issuer=str(issuer_did),
            issuance_date=datetime.now(UTC),
            expiration_date=(
                datetime.now(UTC) + timedelta(days=expires_in_days)
                if expires_in_days
                else None
            ),
            credential_subject=subject,
        )
        credential.proof = self._sign(issuer_subject, issuer_did, credential)
        self.credentials[credential.id] = credential
        return credential

    def _sign(
        self, issuer_subject: str, issuer_did: str, credential: VerifiableCredential
    ) -> dict[str, Any]:
        message = canonical(credential.unsigned_payload)
        signature = self.keystore.sign(issuer_subject, message)
        return {
            "type": str(ProofType.ED25519),
            "created": credential.issuance_date.isoformat(),
            "verificationMethod": f"{issuer_did}#key-1",
            "proofPurpose": "assertionMethod",
            "proofValue": signature,
        }

    def verify_credential(self, credential: VerifiableCredential) -> dict[str, Any]:
        """Verify signature, issuer, expiry and revocation.

        Returns a structured result rather than a boolean: a verifier that cannot
        say *which* check failed is not much use to whoever has to act on it.
        """
        checks: dict[str, bool] = {}
        problems: list[str] = []

        document = self.resolver.resolve(credential.issuer)
        checks["issuerResolved"] = document is not None
        if document is None:
            problems.append(f"issuer DID {credential.issuer} could not be resolved")
            return {"valid": False, "checks": checks, "problems": problems, "issuer": credential.issuer}

        verification_method = credential.proof.get("verificationMethod", "")
        public_key = document.public_key_for(verification_method)
        checks["verificationMethodPresent"] = public_key is not None
        if public_key is None:
            problems.append(f"verification method {verification_method} is not in the issuer's document")
            return {"valid": False, "checks": checks, "problems": problems, "issuer": credential.issuer}

        issuer_subject = self.resolver.subject_for(credential.issuer)
        checks["issuerKnown"] = issuer_subject is not None
        signature_valid = (
            self.keystore.verify(
                public_key, canonical(credential.unsigned_payload), credential.proof.get("proofValue", "")
            )
            if issuer_subject is not None
            else False
        )
        checks["signatureValid"] = signature_valid
        if not signature_valid:
            problems.append("the credential signature does not verify against the issuer's key")

        checks["notExpired"] = not credential.is_expired
        if credential.is_expired:
            problems.append(f"credential expired at {credential.expiration_date}")

        checks["notRevoked"] = not self.revocations.is_revoked(credential.id)
        if not checks["notRevoked"]:
            entry = self.revocations.entry(credential.id) or {}
            problems.append(
                f"credential was revoked by {entry.get('revokedBy', 'unknown')}: {entry.get('reason', '')}"
            )

        checks["statusActive"] = credential.status is VerifiableCredentialStatus.ACTIVE
        if not checks["statusActive"]:
            problems.append(f"credential status is {credential.status}")

        return {
            "valid": all(checks.values()),
            "checks": checks,
            "problems": problems,
            "issuer": credential.issuer,
            "subject": credential.credential_subject.get("id"),
        }

    def revoke(self, credential_id: str, reason: str, revoked_by: str) -> dict[str, Any]:
        """Revoke a credential, keeping the issuance record intact."""
        credential = self.credentials.get(credential_id)
        if credential is None:
            msg = f"unknown credential '{credential_id}'"
            raise KeyError(msg)
        entry = self.revocations.revoke(credential_id, reason, revoked_by)
        credential.status = VerifiableCredentialStatus.REVOKED
        return entry

    def credential_for(self, credential_id: str) -> VerifiableCredential | None:
        return self.credentials.get(credential_id)


__all__ = [
    "CredentialRevocationRegistry",
    "DIDResolver",
    "DidKeyResolver",
    "IdentityService",
]