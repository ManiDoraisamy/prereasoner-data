"""List popular Socrata datasets on a domain with license and column count.
Usage: python -I socrata_list.py <domain> [query] [limit]"""
import sys
sys.path.append("C:/Users/manid/AppData/Roaming/Python/Python314/site-packages")
import requests  # noqa: E402

domain = sys.argv[1]
q = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 40
params = {"domains": domain, "search_context": domain, "only": "datasets", "limit": limit, "order": "page_views_total"}
if q:
    params["q"] = q
r = requests.get("https://api.us.socrata.com/api/catalog/v1", params=params, timeout=60)
for res in r.json().get("results", []):
    m = res.get("metadata", {})
    rs = res.get("resource", {})
    print(f"{rs.get('id')}\t{len(rs.get('columns_name', []))}c\t{(m.get('license') or '-')[:28]}\t{rs.get('updatedAt','')[:10]}\t{rs.get('name','')[:70]}")
