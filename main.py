from __future__ import annotations

from typing import Optional, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import firebase_admin
from firebase_admin import credentials, firestore, messaging, exceptions as fb_exceptions

# =========================================================
#  FASTAPI
# =========================================================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en prod : restreindre
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
#  FIREBASE ADMIN
# =========================================================

# ⚠️ Chemin vers la clé de compte de service du projet telechat-01
SERVICE_ACCOUNT_PATH = "serviceAccountKey.json"

cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)

db = firestore.client()

# =========================================================
#  MODELES
# =========================================================

class FcmRegisterRequest(BaseModel):
    user_id: str
    fcm_token: str


class SendMessageNotifRequest(BaseModel):
    sender_id: str
    chat_id: str
    content: str


class TestPushRequest(BaseModel):
    token: str
    title: Optional[str] = "Test TeleChat"
    body: Optional[str] = "Ceci est une notification de test."


# =========================================================
#  HELPERS
# =========================================================

def send_push_to_fcm_token(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> str:
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
    )
    print(">>> Envoi FCM vers token:", token)
    try:
        response = messaging.send(message)
        print(">>> Réponse FCM message_id:", response)
        return response
    except fb_exceptions.UnregisteredError:
        print("!!! Token FCM unregistered (mort):", token)
        raise
    except Exception as e:
        print("!!! Erreur générale FCM:", repr(e))
        raise


def get_chat_metadata(chat_id: str) -> Dict[str, object]:
    """
    Lit chats/{chat_id} et retourne:
    - type: 'individual' ou 'group'
    - name: nom du chat / groupe
    - photoURL: groupPhotoUrl si groupe (jamais la photo d'un user)
    - participants: liste des uids
    """
    chat_ref = db.collection("chats").document(chat_id)
    doc = chat_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Chat introuvable")

    data = doc.to_dict() or {}
    chat_type = data.get("type", "individual")
    participants: List[str] = data.get("participants") or data.get("members") or []

    name = data.get("name") or ("Groupe" if chat_type == "group" else "Utilisateur")
    photo = ""
    if chat_type == "group":
        # 👉 pour un groupe on prend UNIQUEMENT la photo du groupe
        photo = data.get("groupPhotoUrl") or ""

    return {
        "type": chat_type,
        "name": name,
        "photoURL": photo,
        "participants": participants,
    }


def get_receiver_id_from_chat_participants(participants: List[str], sender_id: str) -> str:
    if not isinstance(participants, list) or len(participants) < 2:
        raise HTTPException(status_code=400, detail="Participants du chat invalides")

    others = [uid for uid in participants if uid != sender_id]
    if not others:
        raise HTTPException(status_code=400, detail="Impossible de trouver le destinataire")

    return others[0]


def get_user_profile(user_id: str) -> Dict[str, str]:
    """
    Lit users/{user_id} pour récupérer le nom + la photo.
    - nom : champs 'name' ou 'displayName'
    - photo : champ 'profilePhotoUrl'
    """
    doc_ref = db.collection("users").document(user_id)
    doc = doc_ref.get()

    if not doc.exists:
        print(f"!!! Profil user {user_id} introuvable dans 'users'")
        return {
            "displayName": "Utilisateur",
            "photoURL": ""
        }

    data = doc.to_dict() or {}
    display_name = data.get("name") or data.get("displayName") or "Utilisateur"
    photo_url = data.get("profilePhotoUrl") or ""

    return {
        "displayName": display_name,
        "photoURL": photo_url
    }


def get_fcm_token_for_user(user_id: str) -> Optional[str]:
    doc_ref = db.collection("userTokens").document(user_id)
    doc = doc_ref.get()

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    token = data.get("token")
    return token


# =========================================================
#  ENDPOINTS
# =========================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Backend TeleChat (FCM + Firestore) en ligne"
    }


@app.post("/api/fcm/register")
async def register_fcm_token(req: FcmRegisterRequest):
    try:
        print(">>> Enregistrement token FCM pour user:", req.user_id)
        db.collection("userTokens").document(req.user_id).set(
            {
                "token": req.fcm_token,
                "userId": req.user_id,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"success": True}
    except Exception as e:
        print("!!! Erreur Firestore register_fcm_token:", e)
        raise HTTPException(status_code=500, detail=f"Erreur Firestore: {e}")


@app.post("/api/messages/send-notif")
async def send_message_notification(req: SendMessageNotifRequest):
    """
    Envoie une notification quand un message est envoyé :
    - Chat privé : titre = nom réel de l'expéditeur, icône = photo de l'expéditeur
    - Groupe : titre = nom du groupe, icône = photo du groupe,
               body = "NomExpéditeur : message"
    """
    print(">>> send-notif reçu:", req.dict())

    chat_meta = get_chat_metadata(req.chat_id)
    chat_type = chat_meta["type"]
    chat_name = chat_meta["name"]
    chat_photo = chat_meta["photoURL"]
    participants: List[str] = chat_meta["participants"]

    # Destinataire
    if chat_type == "individual":
        receiver_id = get_receiver_id_from_chat_participants(participants, req.sender_id)
    else:
        other_members = [uid for uid in participants if uid != req.sender_id]
        if not other_members:
            raise HTTPException(status_code=400, detail="Aucun autre membre dans le groupe")
        # Ici on en notifie un seul (à étendre si tu veux tous les membres)
        receiver_id = other_members[0]
    print(">>> Destinataire calculé:", receiver_id)

    token = get_fcm_token_for_user(receiver_id)
    print(">>> Token FCM destinataire:", token)
    if not token:
        return {
            "success": False,
            "reason": "Destinataire sans token FCM",
            "receiver_id": receiver_id,
        }

    sender_profile = get_user_profile(req.sender_id)
    sender_name = sender_profile["displayName"]
    sender_photo = sender_profile["photoURL"]

    preview_plain = req.content[:120]

    if chat_type == "group":
        title = chat_name
        body = f"{sender_name}: {preview_plain}"
        # icône côté client = groupPhotoUrl (chat_photo)
    else:
        title = sender_name
        body = preview_plain
        # icône côté client = photo de l'expéditeur (sender_photo)

    # On envoie les deux photos, le SW web choisira
    data = {
        "type": "chat_message",
        "chat_id": req.chat_id,
        "chat_type": chat_type,
        "sender_id": req.sender_id,
        "receiver_id": receiver_id,
        "sender_name": sender_name,
        "sender_photo": sender_photo or "",
        "chat_name": chat_name,
        "chat_photo": chat_photo or "",
        "preview": preview_plain,
    }

    try:
        message_id = send_push_to_fcm_token(
            token=token,
            title=title,
            body=body,
            data=data,
        )
        return {
            "success": True,
            "message_id": message_id,
            "receiver_id": receiver_id,
        }
    except Exception as e:
        print("!!! Erreur FCM dans send_message_notification:", repr(e))
        raise HTTPException(status_code=500, detail=f"Erreur FCM: {e}")


@app.post("/api/fcm/test-push")
async def test_push(req: TestPushRequest):
    print(">>> test-push vers token:", req.token)
    try:
        message_id = send_push_to_fcm_token(
            token=req.token,
            title=req.title,
            body=req.body,
            data={"type": "test"},
        )
        return {"success": True, "message_id": message_id}
    except Exception as e:
        print("!!! Erreur FCM dans test-push:", repr(e))
        raise HTTPException(status_code=500, detail=f"Erreur FCM: {e}")
