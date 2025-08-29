FROM python:3.11-slim
WORKDIR /app

# 1) System CA store
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Optional: help libs find the default CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# 2) Download DigiCert intermediate that signs *.air-e.com
#    (Issuer shown in your screenshot: "DigiCert Global G2 TLS RSA SHA256 2020 CA1")
ADD https://cacerts.digicert.com/DigiCertGlobalG2TLSRSASHA2562020CA1.crt.pem /app/digicert_intermediate.pem

# 3) Build a combined bundle: system roots + DigiCert intermediate
RUN cat /etc/ssl/certs/ca-certificates.crt /app/digicert_intermediate.pem > /app/aire_ca_bundle.pem

# 4) Expose the path to Python (we'll read this in code)
ENV AIRE_CA_BUNDLE=/app/aire_ca_bundle.pem

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]