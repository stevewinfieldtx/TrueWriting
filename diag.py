"""Quick diagnostic: test each Outlook account's Sent Items access speed."""
import win32com.client
import pythoncom
import time

pythoncom.CoInitialize()
outlook = win32com.client.Dispatch("Outlook.Application")
ns = outlook.GetNamespace("MAPI")

print(f"Outlook has {ns.Folders.Count} accounts\n")

for i in range(1, ns.Folders.Count + 1):
    acct = ns.Folders.Item(i)
    print(f"Account {i}: {acct.Name}")
    
    # Find Sent Items
    sent = None
    try:
        for j in range(1, acct.Folders.Count + 1):
            sub = acct.Folders.Item(j)
            if sub.Name in ['Sent Items', 'Sent Mail', 'Sent']:
                sent = sub
                break
    except Exception as e:
        print(f"  ERROR listing folders: {e}\n")
        continue
    
    if not sent:
        print(f"  No Sent Items folder found\n")
        continue
    
    t0 = time.time()
    try:
        count = sent.Items.Count
        print(f"  Items.Count = {count} ({time.time()-t0:.1f}s)")
    except Exception as e:
        print(f"  ERROR getting count: {e} ({time.time()-t0:.1f}s)\n")
        continue
    
    if count == 0:
        print(f"  Empty - skipping\n")
        continue
    
    # Try to read just the FIRST item
    t0 = time.time()
    try:
        item = sent.Items.GetFirst()
        print(f"  GetFirst() OK ({time.time()-t0:.1f}s)")
        
        t0 = time.time()
        subj = item.Subject
        print(f"  Subject: {subj[:50]} ({time.time()-t0:.1f}s)")
        
        t0 = time.time()
        body_len = len(item.Body) if item.Body else 0
        print(f"  Body length: {body_len} chars ({time.time()-t0:.1f}s)")
    except Exception as e:
        print(f"  ERROR reading first item: {e} ({time.time()-t0:.1f}s)")
    
    print()

pythoncom.CoUninitialize()
print("Done.")
