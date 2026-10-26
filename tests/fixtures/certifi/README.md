# Public revoked-trust regression fixture

`emsign-ecc-root-g3.pem` is the public **emSign ECC Root CA - G3**
certificate, extracted unchanged from the official PyPI wheel
`certifi-2026.7.22-py3-none-any.whl` (SHA256
`62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775`).
It contains no private key. It is test data only and is not copied into the
application image or added to application trust.

Its DER SHA256 is
`86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`.
The fixture checks that the actual certifi bundle rejects the distrusted root,
even when installed metadata claims the fixed version.

Sources:
- [Maintainer advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
- [Reviewed advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
- [2026.10.19 release](https://github.com/certifi/python-certifi/releases/tag/2026.10.19)

The first fix, 2026.10.18, removed all emSign roots. The pinned 2026.10.19
release restores the three unaffected emSign roots. Tests preserve those
roots and ordinary ISRG/DigiCert trust. They make no remote TLS requests and
do not claim that every production endpoint uses certifi rather than another
trust store. The separately frozen historical capacity baseline is unchanged.

The offline CLI check uses `openssl verify -trusted`, which explicitly limits
trust anchors to the selected bundle. Supplying `-CAfile` plus an empty
`-CApath` alone leaves OpenSSL 3's default CAstore enabled. The tests also
inject an unsafe default certificate-file environment and keep the required
failure specific to verification error 18, not arbitrary command failure.
The image checker uses an initially empty Python `SSLContext` and never loads
system defaults.
