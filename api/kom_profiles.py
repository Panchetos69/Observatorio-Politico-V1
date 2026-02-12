import os
import json
from typing import Dict, Any

class KomProfiles:
    def __init__(self, data_dir: str):
        self.base = os.path.join(data_dir, "kom_profiles")

    def _path(self, chamber: str, person_id: str) -> str:
        return os.path.join(self.base, chamber, f"{person_id}.json")

    def get(self, chamber: str, person_id: str) -> Dict[str, Any]:
        p = self._path(chamber, person_id)
        if not os.path.exists(p):
            return {"success": True, "exists": False, "profile": {"notas": "", "tags": [], "links": []}}
        with open(p, "r", encoding="utf-8") as f:
            return {"success": True, "exists": True, "profile": json.load(f)}

    def upsert(self, chamber: str, person_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        os.makedirs(os.path.join(self.base, chamber), exist_ok=True)
        p = self._path(chamber, person_id)
        profile = {
            "person_id": person_id,
            "chamber": chamber,
            "notas": payload.get("notas", ""),
            "tags": payload.get("tags", []),
            "links": payload.get("links", []),
            "updated_at": payload.get("updated_at", "")
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        return {"success": True, "saved": True, "profile": profile}