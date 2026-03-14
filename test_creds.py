from pocketpaw.credentials import get_credential_store

store = get_credential_store()
print(store.get_all())
