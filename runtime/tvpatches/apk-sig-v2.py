#!/usr/bin/env python3
"""PackageManager reports the signing certificate of APKs signed only with APK Signature Scheme v2/v3 (no
META-INF JAR signature), as Android does. ATL read only JAR (v1) signatures, so such apps got no signatures and
SigningInfo.getApkContentsSigners() was always null. Apps that send their signing-cert hash to their backend (WuPlay:
X-Wuplay-Signing-Cert) were rejected as "modified version". Now:
- collectCertificates() reads the signer certificate from the APK Signing Block (v3.1, v3, then v2; no signature
  verification, like ATL's trusted path for v1) and falls back to the JAR path;
- PackageInfo.signingInfo carries the signatures (getApkContentsSigners / getSigningCertificateHistory).
usage: apk-sig-v2.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl/android/content/pm")


def edit(f, old, new, marker):
    p = os.path.join(R, f)
    s = open(p).read()
    if marker in s:
        return
    assert old in s, (f, old[:60])
    open(p, "w").write(s.replace(old, new, 1))


open(os.path.join(R, "ApkSigBlock.java"), "w").write('''package android.content.pm;

import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** tv: reads the first signer's certificates (X.509 DER) from an APK Signing Block (v3.1, v3, v2). Not verified. */
final class ApkSigBlock {
	private static final int[] IDS = {0x1b93ad61, 0xf05368c0, 0x7109871a};

	static byte[][] signerCerts(String path) {
		try (RandomAccessFile f = new RandomAccessFile(path, "r")) {
			long len = f.length();
			int tail = (int)Math.min(len, 65557);
			byte[] t = new byte[tail];
			f.seek(len - tail);
			f.readFully(t);
			ByteBuffer tb = ByteBuffer.wrap(t).order(ByteOrder.LITTLE_ENDIAN);
			int eocd = -1;
			for (int i = tail - 22; i >= 0; i--)
				if (tb.getInt(i) == 0x06054b50) {
					eocd = i;
					break;
				}
			if (eocd < 0)
				return null;
			long cd = tb.getInt(eocd + 16) & 0xffffffffL;
			if (cd < 32)
				return null;
			byte[] foot = new byte[24];
			f.seek(cd - 24);
			f.readFully(foot);
			ByteBuffer fb = ByteBuffer.wrap(foot).order(ByteOrder.LITTLE_ENDIAN);
			if (fb.getLong(8) != 0x20676953204b5041L || fb.getLong(16) != 0x3234206b636f6c42L) // "APK Sig Block 42"
				return null;
			long size = fb.getLong(0);
			if (size < 24 || size > cd - 8 || size > (64 << 20))
				return null;
			byte[] blk = new byte[(int)size - 24];
			f.seek(cd - size);
			f.readFully(blk);
			ByteBuffer pairs = ByteBuffer.wrap(blk).order(ByteOrder.LITTLE_ENDIAN);
			for (int want : IDS) {
				pairs.position(0);
				while (pairs.remaining() >= 12) {
					long plen = pairs.getLong();
					if (plen < 4 || plen > pairs.remaining())
						break;
					int next = pairs.position() + (int)plen;
					int id = pairs.getInt();
					if (id == want) {
						ByteBuffer v = slice(pairs, next - pairs.position());
						byte[][] c = firstSigner(v);
						if (c != null && c.length > 0)
							return c;
					}
					pairs.position(next);
				}
			}
		} catch (Exception e) {
			System.err.println("ApkSigBlock: " + path + ": " + e);
		}
		return null;
	}

	private static ByteBuffer slice(ByteBuffer b, int n) {
		ByteBuffer s = b.slice().order(ByteOrder.LITTLE_ENDIAN);
		s.limit(n);
		b.position(b.position() + n);
		return s;
	}

	private static ByteBuffer prefixed(ByteBuffer b) {
		int n = b.getInt();
		if (n < 0 || n > b.remaining())
			throw new IllegalArgumentException("bad length");
		return slice(b, n);
	}

	private static byte[][] firstSigner(ByteBuffer v) {
		ByteBuffer signers = prefixed(v);
		if (!signers.hasRemaining())
			return null;
		ByteBuffer signer = prefixed(signers);
		ByteBuffer signedData = prefixed(signer);
		prefixed(signedData); // digests
		ByteBuffer certs = prefixed(signedData);
		java.util.ArrayList<byte[]> out = new java.util.ArrayList<>();
		while (certs.hasRemaining()) {
			ByteBuffer c = prefixed(certs);
			byte[] der = new byte[c.remaining()];
			c.get(der);
			out.add(der);
		}
		return out.toArray(new byte[0][]);
	}
}
''')

mb = os.path.join(sys.argv[1], "src/api-impl/meson.build")
m = open(mb).read()
if "ApkSigBlock.java" not in m:
    assert "\t'android/content/pm/PackageParser.java',\n" in m
    m = m.replace("\t'android/content/pm/PackageParser.java',\n", "\t'android/content/pm/ApkSigBlock.java',\n\t'android/content/pm/PackageParser.java',\n", 1)
    open(mb, "w").write(m)

edit("PackageParser.java", """	public boolean collectCertificates(Package pkg, int flags) {
		pkg.mSignatures = null;
""", """	public boolean collectCertificates(Package pkg, int flags) {
		pkg.mSignatures = null;
		byte[][] v2 = ApkSigBlock.signerCerts(mArchiveSourcePath); // tv: APK Signature Scheme v2/v3
		if (v2 != null) {
			pkg.mSignatures = new Signature[v2.length];
			pkg.mSigningKeys = new HashSet<PublicKey>();
			for (int i = 0; i < v2.length; i++) {
				pkg.mSignatures[i] = new Signature(v2[i]);
				try {
					pkg.mSigningKeys.add(java.security.cert.CertificateFactory.getInstance("X.509")
					    .generateCertificate(new java.io.ByteArrayInputStream(v2[i])).getPublicKey());
				} catch (Exception e) {
				}
			}
			return true;
		}
""", "ApkSigBlock.signerCerts")

edit("PackageParser.java", """		if ((flags & PackageManager.GET_SIGNATURES) != 0) {
			int N = (p.mSignatures != null) ? p.mSignatures.length : 0;""", """		if (p.mSignatures != null && p.mSignatures.length > 0) // tv: GET_SIGNING_CERTIFICATES
			pi.signingInfo = new SigningInfo(p.mSignatures);
		if ((flags & PackageManager.GET_SIGNATURES) != 0) {
			int N = (p.mSignatures != null) ? p.mSignatures.length : 0;""", "new SigningInfo(p.mSignatures)")

edit("SigningInfo.java", """public final class SigningInfo {
	public Signature[] getApkContentsSigners() {
		return null;
	}
""", """public final class SigningInfo {
	private Signature[] signers; // tv: from PackageParser.collectCertificates

	public SigningInfo() {}

	public SigningInfo(Signature[] signers) {
		this.signers = signers;
	}

	public Signature[] getApkContentsSigners() {
		return signers;
	}
""", "private Signature[] signers")
edit("SigningInfo.java", """	public Signature[] getSigningCertificateHistory() {
		return null;
	}""", """	public Signature[] getSigningCertificateHistory() {
		return signers;
	}""", "getSigningCertificateHistory() {\n\t\treturn signers;")
