import os, sys

# Load .env manually
env_vars = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env_vars[k.strip()] = v.strip()
            os.environ[k.strip()] = v.strip()

print('Loaded from .env:')
print('  TENANT_ID:', env_vars.get('AZURE_TENANT_ID'))
print('  CLIENT_ID:', env_vars.get('AZURE_CLIENT_ID'))
secret = env_vars.get('AZURE_CLIENT_SECRET', '')
print('  SECRET (first 10):', secret[:10] + '...')
print('  SECRET length:', len(secret))
print('  MAILBOX:', env_vars.get('GRAPH_SENDER_MAILBOX'))

import msal

tenant_id = env_vars['AZURE_TENANT_ID']
client_id = env_vars['AZURE_CLIENT_ID']
client_secret = env_vars['AZURE_CLIENT_SECRET']
authority = 'https://login.microsoftonline.com/' + tenant_id

print('\nAuthority:', authority)

app = msal.ConfidentialClientApplication(
    client_id=client_id,
    client_credential=client_secret,
    authority=authority
)
result = app.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])

if 'access_token' in result:
    print('TOKEN SUCCESS! Type:', result['token_type'])
else:
    print('ERROR:', result.get('error'))
    print('DESCRIPTION:', result.get('error_description', '')[:400])
