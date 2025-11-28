from __future__ import annotations

from typing import Optional, Dict, List

import os
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import firebase_admin
from firebase_admin import credentials, firestore, messaging, exceptions as fb_exceptions

# =========================================================
#  LOGGING
# =========================================================

logger = logging.getLogger("telechat-backend")
logging.basicConfig(level=logging.INFO)


# =========================================================
#  FASTAPI
# =========================================================

app = FastAPI(title="TeleChat Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ⚠️ en prod : restreindre
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
#  FIREBASE ADMIN
# =========================================================

# Chemin vers la clé de compte de service
# Tu peux aussi le passer par variable d'environnement TELECHAT_SERVICE_ACCOUNT
SERVICE_ACCOUNT_PATH = os.getenv("TELECHAT_SERVICE_ACCOUNT", "serviceAccountKey.json")

if not os.path.exists(SERVICE_ACCOUNT_PATH):
    logger.error(
        "Fichier service account introuvable: %s\n"
        "⚠️ Vérifie que tu as bien copié le JSON téléchargé depuis Firebase.",
        SERVICE_ACCOUNT_PATH,
    )

firebase_app: Optional[firebase_admin.App] = None
db: firestore.Client | None = None

try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin initialisé avec succès.")
    else:
        # Réutiliser une app existante si le code est rechargé (ex: --reload)
        firebase_app = list(firebase_admin._apps.values())[0]
        logger.info("Firebase Admin déjà initialisé, réutilisation de l'instance existante.")

    db = firestore.client()
    logger.info("Client Firestore initialisé.")
except Exception as e:
    logger.exception("Erreur lors de l'initialisation de Firebase Admin / Firestore: %r", e)
    # On laisse tourner l'API, mais tous les endpoints qui utilisent Firestore devront vérifier db


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

def ensure_db() -> firestore.Client:
    """
    Vérifie que Firestore est bien initialisé.
    Lève une HTTPException 500 sinon.
    """
    if db is None:
        logger.error("Firestore non initialisé (db est None).")
        raise HTTPException(
            status_code=500,
            detail="Firestore non initialisé côté serveur. Vérifie la clé de service Firebase.",
        )
    return db


def send_push_to_fcm_token(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> str:
    """
    Envoie une notification FCM à un token.
    Lève une exception si l'envoi échoue.
    """
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
    )
    logger.info(">>> Envoi FCM vers token: %s", token)

    try:
        response = messaging.send(message)
        logger.info(">>> Réponse FCM message_id: %s", response)
        return response
    except fb_exceptions.UnregisteredError:
        logger.warning("!!! Token FCM unregistered (mort): %s", token)
        # À toi de décider : supprimer le token en base ici si tu veux
        raise
    except Exception as e:
        logger.exception("!!! Erreur générale FCM: %r", e)
        raise


def get_chat_metadata(chat_id: str) -> Dict[str, object]:
    """
    Lit chats/{chat_id} et retourne:
    - type: 'individual' ou 'group'
    - name: nom du chat / groupe
    - photoURL: groupPhotoUrl si groupe (jamais la photo d'un user)
    - participants: liste des uids
    """
    client = ensure_db()
    chat_ref = client.collection("chats").document(chat_id)
    doc = chat_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Chat introuvable")

    data = doc.to_dict() or {}
    chat_type = data.get("type", "individual")
    participants: List[str] = data.get("participants") or data.get("members") or []

    name = data.get("name") or ("Groupe" if chat_type == "group" else "Utilisateur")
    photo = ""
    if chat_type == "group":
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
    client = ensure_db()
    doc_ref = client.collection("users").document(user_id)
    doc = doc_ref.get()

    if not doc.exists:
        logger.warning("!!! Profil user %s introuvable dans 'users'", user_id)
        return {
            "displayName": "Utilisateur",
            "photoURL": "",
        }

    data = doc.to_dict() or {}
    display_name = data.get("name") or data.get("displayName") or "Utilisateur"
    photo_url = data.get("profilePhotoUrl") or ""

    return {
        "displayName": display_name,
        "photoURL": photo_url,
    }


def get_fcm_token_for_user(user_id: str) -> Optional[str]:
    client = ensure_db()
    doc_ref = client.collection("userTokens").document(user_id)
    doc = doc_ref.get()

    if not doc.exists:
        logger.info("Aucun token FCM trouvé pour user_id=%s", user_id)
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
        "message": "Backend TeleChat (FCM + Firestore) en ligne",
    }


@app.post("/api/fcm/register")
async def register_fcm_token(req: FcmRegisterRequest):
    client = ensure_db()
    try:
        logger.info(">>> Enregistrement token FCM pour user: %s", req.user_id)
        client.collection("userTokens").document(req.user_id).set(
            {
                "token": req.fcm_token,
                "userId": req.user_id,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"success": True}
    except Exception as e:
        logger.exception("!!! Erreur Firestore register_fcm_token: %r", e)
        raise HTTPException(status_code=500, detail=f"Erreur Firestore: {e}")


@app.post("/api/messages/send-notif")
async def send_message_notification(req: SendMessageNotifRequest):
    """
    Envoie une notification quand un message est envoyé :
    - Chat privé : titre = nom réel de l'expéditeur, icône = photo de l'expéditeur
    - Groupe : titre = nom du groupe, icône = photo du groupe,
               body = "NomExpéditeur : message"
    """
    logger.info(">>> send-notif reçu: %s", req.dict())

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
        # Ici on en notifie un seul (à étendre pour tous les membres si tu veux)
        receiver_id = other_members[0]

    logger.info(">>> Destinataire calculé: %s", receiver_id)

    token = get_fcm_token_for_user(receiver_id)
    logger.info(">>> Token FCM destinataire: %s", token)
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
        logger.exception("!!! Erreur FCM dans send_message_notification: %r", e)
        raise HTTPException(status_code=500, detail=f"Erreur FCM: {e}")


@app.post("/api/fcm/test-push")
async def test_push(req: TestPushRequest):
    logger.info(">>> test-push vers token: %s", req.token)
    try:
        message_id = send_push_to_fcm_token(
            token=req.token,
            title=req.title,
            body=req.body,
            data={"type": "test"},
        )
        return {"success": True, "message_id": message_id}
    except Exception as e:
        logger.exception("!!! Erreur FCM dans test-push: %r", e)
        raise HTTPException(status_code=500, detail=f"Erreur FCM: {e}")
