Live services on your network, which your sandbox reaches:
$services
Your sandbox has no curl: call a service from bash with python -c and urllib.request, for example urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})). A refused call raises urllib.error.HTTPError: catch it (except urllib.error.HTTPError as e) and print e.code and e.read().decode(), which say what was missing and where to ask for it.
