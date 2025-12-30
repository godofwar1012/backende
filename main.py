from __future__ import annotations

import os
from typing import Optional, Dict, List, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import firebase_admin
from firebase_admin import credentials, firestore, messaging, exceptions as fb_exceptions
from google.cloud import firestore as gcfirestore  # pour SERVER_TIMESTAMP

# =========================================================
#  FASTAPI
# =========================================================

app = FastAPI(title="TeleChat Backend", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ⚠️ à restreindre en prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
#  FIREBASE ADMIN
# =========================================================

def _bootstrap_firebase():
    """
    Initialise Firebase Admin à partir d'un chemin de clé ou des variables d'env.
    Variables supportées :
      - SERVICE_ACCOUNT_PATH
      - GOOGLE_APPLICATION_CREDENTIALS
    Fallback : initialise sans fichier si des identifiants d'environnement existent.
    """
    svc_path = (
        os.getenv("SERVICE_ACCOUNT_PATH")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or "serviceAccountKey.json"
    )
    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate(svc_path)
            firebase_admin.initialize_app(cred)
            print(f"[BOOT] Firebase Admin initialisé avec {svc_path}")
        except Exception as e:
            firebase_admin.initialize_app()
            print(f"[BOOT] Firebase Admin initialisé via env. Motif: {e!r}")


_bootstrap_firebase()
db = firestore.client()

# =========================================================
#  MODÈLES REQUÊTES
# =========================================================

class FcmRegisterRequest(BaseModel):
    user_id: str = Field(..., description="UID Firebase du user")
    fcm_token: str = Field(..., description="Token Web FCM obtenu via messaging.getToken()")


class SendMessageNotifRequest(BaseModel):
    sender_id: str
    chat_id: str
    content: str = Field(..., description="Texte du message pour l'aperçu (le SW appliquera previewMode)")


class TestPushRequest(BaseModel):
    token: str
    title: Optional[str] = "Test TeleChat"
    body: Optional[str] = "Ceci est une notification de test."

# =========================================================
#  CONSTANTES & DÉFAUTS (réglages cross-device)
# =========================================================

DEFAULT_NOTIFICATION_SETTINGS: Dict[str, object] = {
    "enabled": True,
    "groupsEnabled": True,
    "contactsEnabled": True,
    "previewMode": "full",  # 'full' | 'name-only' | 'none'
    "ignoredGroups": [],
    "ignoredContacts": [],
    "mutedChats": [],
}

# =========================================================
#  HELPERS FIRESTORE
# =========================================================

def get_chat_metadata(chat_id: str) -> Dict[str, object]:
    """
    Lit chats/{chat_id} et retourne:
      - type: 'individual' | 'group'
      - name: nom du chat / du groupe
      - photoURL: groupPhotoUrl si groupe, sinon ""
      - participants: liste des uids
    """
    snap = db.collection("chats").document(chat_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Chat introuvable")

    data = snap.to_dict() or {}
    chat_type = data.get("type", "individual")
    participants: List[str] = data.get("participants") or data.get("members") or []
    name = data.get("name") or ("Groupe" if chat_type == "group" else "Utilisateur")
    photo = data.get("groupPhotoUrl") if chat_type == "group" else ""

    return {
        "type": chat_type,
        "name": name,
        "photoURL": photo or "",
        "participants": participants,
    }


def get_user_profile(user_id: str) -> Dict[str, str]:
    """
    Lit users/{user_id} pour récupérer displayName + photoURL
    """
    snap = db.collection("users").document(user_id).get()
    if not snap.exists:
        print(f"[WARN] Profil {user_id} introuvable -> valeurs par défaut")
        return {"displayName": "Utilisateur", "photoURL": ""}

    d = snap.to_dict() or {}
    display_name = d.get("name") or d.get("displayName") or "Utilisateur"
    photo_url = d.get("profilePhotoUrl") or ""
    return {"displayName": display_name, "photoURL": photo_url}


def get_user_notification_settings(user_id: str) -> Dict[str, object]:
    """
    Lit users/{uid}.notificationSettings (+ mutedChats si présent) et fusionne avec les défauts.
    """
    snap = db.collection("users").document(user_id).get()
    if not snap.exists:
        return DEFAULT_NOTIFICATION_SETTINGS.copy()

    d = snap.to_dict() or {}
    s = d.get("notificationSettings") or {}
    out = {**DEFAULT_NOTIFICATION_SETTINGS, **s}

    if "mutedChats" in d and isinstance(d["mutedChats"], list):
        out["mutedChats"] = d["mutedChats"]

    out["ignoredGroups"]   = list(out.get("ignoredGroups") or [])
    out["ignoredContacts"] = list(out.get("ignoredContacts") or [])
    out["mutedChats"]      = list(out.get("mutedChats") or [])

    return out


def get_fcm_token_for_user(user_id: str) -> Optional[str]:
    snap = db.collection("userTokens").document(user_id).get()
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get("token")


def list_group_receivers(participants: List[str], sender_id: str) -> List[str]:
    return [uid for uid in participants if uid and uid != sender_id]


def get_individual_receiver(participants: List[str], sender_id: str) -> str:
    if not isinstance(participants, list) or len(participants) < 2:
        raise HTTPException(status_code=400, detail="Participants du chat invalides")
    others = [uid for uid in participants if uid != sender_id]
    if not others:
        raise HTTPException(status_code=400, detail="Impossible de trouver le destinataire")
    return others[0]

# =========================================================
#  POLITIQUE D’ENVOI (barrière serveur)
# =========================================================

def server_should_notify(
    settings: Dict[str, object], *,
    chat_type: str,
    chat_id: str,
    sender_id: str
) -> Tuple[bool, str]:
    """
    Barrière n°1 (serveur) pour éviter les envois si l'utilisateur a coupé.
    Le SW appliquera encore previewMode côté client (barrière n°2).
    """
    if not settings.get("enabled", True):
        return False, "global_off"

    if chat_type == "group" and not settings.get("groupsEnabled", True):
        return False, "groups_off"

    if chat_type != "group" and not settings.get("contactsEnabled", True):
        return False, "contacts_off"

    muted_chats: List[str] = settings.get("mutedChats", []) or []
    if chat_id in muted_chats:
        return False, "chat_muted"

    if chat_type == "group":
        ignored_groups: List[str] = settings.get("ignoredGroups", []) or []
        if chat_id in ignored_groups:
            return False, "group_ignored"
    else:
        ignored_contacts: List[str] = settings.get("ignoredContacts", []) or []
        if sender_id in ignored_contacts:
            return False, "contact_ignored"

    return True, "ok"

# =========================================================
#  ENVOI FCM (DATA-ONLY)
# =========================================================

def send_data_only_to_token(
    token: str,
    data: Dict[str, str],
) -> str:
    """
    Envoi **data-only** (pas de messaging.Notification).
    Le Service Worker décide d'afficher ou non, et applique previewMode.
    """
    msg = messaging.Message(token=token, data=data)
    print(f"[FCM] Data-only → {token[:16]}… | data.keys={list(data.keys())}")
    try:
        res = messaging.send(msg)
        print("[FCM] OK:", res)
        return res
    except fb_exceptions.UnregisteredError:
        print("[FCM] Token unregistered:", token[:24], "…")
        raise
    except Exception as e:
        print("[FCM] Erreur:", repr(e))
        raise


def build_common_data(
    *,
    chat_id: str,
    chat_type: str,
    sender_id: str,
    receiver_id: str,
    sender_name: str,
    sender_photo: str,
    chat_name: str,
    chat_photo: str,
    preview: str,
    title: str,
    body: str,
) -> Dict[str, str]:
    """
    Le SW peut utiliser 'title'/'body' s'il veut (ou les ignorer selon previewMode).
    """
    return {
        "type": "chat_message",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "sender_name": sender_name,
        "sender_photo": sender_photo or "",
        "chat_name": chat_name,
        "chat_photo": chat_photo or "",
        "preview": preview or "",
        "title": title or "",
        "body": body or "",
    }

# =========================================================
#  ENDPOINTS
# =========================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Backend TeleChat (FCM + Firestore) en ligne",
        "version": app.version,
    }


@app.get("/api/version")
async def version():
    return {"version": app.version}


@app.post("/api/fcm/register")
async def register_fcm_token(req: FcmRegisterRequest):
    """
    Enregistre / met à jour le token FCM d’un utilisateur.
    """
    try:
        print(f"[API] Register token user={req.user_id} | token={req.fcm_token[:32]}…")
        db.collection("userTokens").document(req.user_id).set(
            {
                "token": req.fcm_token,
                "userId": req.user_id,
                "updatedAt": gcfirestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"success": True}
    except Exception as e:
        print("[ERR] Firestore register_fcm_token:", e)
        raise HTTPException(status_code=500, detail=f"Erreur Firestore: {e}")


@app.post("/api/fcm/test-push")
async def test_push(req: TestPushRequest):
    """
    Envoi d’un data-only vers un token donné (pour tests).
    """
    print(f"[API] Test push → token={req.token[:16]}…")
    try:
        data = {
            "type": "test",
            "title": req.title or "Test",
            "body": req.body or "",
        }
        msg_id = send_data_only_to_token(token=req.token, data=data)
        return {"success": True, "message_id": msg_id}
    except Exception as e:
        print("[ERR] test-push:", repr(e))
        raise HTTPException(status_code=500, detail=f"Erreur FCM: {e}")


@app.post("/api/messages/send-notif")
async def send_message_notification(req: SendMessageNotifRequest):
    """
    Envoie une **notification data-only** quand un message est envoyé.

    Règles :
      - 1:1 → notifie l'autre participant uniquement.
      - Groupe → notifie tous les membres sauf l'émetteur.
      - Respect des réglages côté destinataire (global OFF, Groupes OFF, Contacts OFF, muted/ignored).
      - Le SW (service worker) applique previewMode et décide d’afficher/masquer.
    Réponse :
      {
        success: true,
        chat_id, chat_type,
        sent:    [{ receiver_id, message_id }],
        skipped: [{ receiver_id, reason }]
      }
    """
    print("[API] send-notif reçu:", req.dict())

    # Métadonnées chat
    meta = get_chat_metadata(req.chat_id)
    chat_type = meta["type"]
    chat_name = meta["name"]
    chat_photo = meta["photoURL"]
    participants: List[str] = meta["participants"]

    # Participants ciblés
    if chat_type == "group":
        target_uids = list_group_receivers(participants, req.sender_id)
    else:
        target_uids = [get_individual_receiver(participants, req.sender_id)]

    if not target_uids:
        raise HTTPException(status_code=400, detail="Aucun destinataire")

    # Profil émetteur
    sender = get_user_profile(req.sender_id)
    sender_name = sender["displayName"]
    sender_photo = sender["photoURL"]

    preview_plain = (req.content or "")[:120]

    if chat_type == "group":
        title_base = chat_name
        body_base = f"{sender_name}: {preview_plain}"
    else:
        title_base = sender_name
        body_base = preview_plain

    sent: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for receiver_id in target_uids:
        try:
            settings = get_user_notification_settings(receiver_id)
            ok, reason = server_should_notify(
                settings, chat_type=chat_type, chat_id=req.chat_id, sender_id=req.sender_id
            )
            if not ok:
                skipped.append({"receiver_id": receiver_id, "reason": reason})
                continue

            token = get_fcm_token_for_user(receiver_id)
            if not token:
                skipped.append({"receiver_id": receiver_id, "reason": "no_token"})
                continue

            data = build_common_data(
                chat_id=req.chat_id,
                chat_type=chat_type,
                sender_id=req.sender_id,
                receiver_id=receiver_id,
                sender_name=sender_name,
                sender_photo=sender_photo,
                chat_name=chat_name,
                chat_photo=chat_photo,
                preview=preview_plain,
                title=title_base,
                body=body_base,
            )

            msg_id = send_data_only_to_token(token=token, data=data)
            sent.append({"receiver_id": receiver_id, "message_id": msg_id})

        except Exception as e:
            print(f"[ERR] Envoi vers {receiver_id}: {e!r}")
            skipped.append({"receiver_id": receiver_id, "reason": "send_error"})

    return {
        "success": True,
        "chat_id": req.chat_id,
        "chat_type": chat_type,
        "sent": sent,
        "skipped": skipped,
    }

