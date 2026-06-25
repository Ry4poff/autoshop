# MRSHOP Telegram WebApp Bot

Bot Telegram simple qui ouvre le site `https://mrshop.astck.com` directement en WebApp Telegram.

## Fichiers

```txt
main.py
requirements.txt
railpack.json
.env.example
.gitignore
README.md
```

## Variables à mettre sur Railway

Dans Railway > Variables :

```env
BOT_TOKEN=ton_token_telegram
WEBAPP_URL=https://mrshop.astck.com
BOT_NAME=MRSHOP
```

Ne mets jamais ton vrai token dans GitHub.

## Commande de lancement

Railway utilisera automatiquement :

```txt
python main.py
```

grâce au fichier `railpack.json`.

## Commandes Telegram

```txt
/start
/shop
/help
```

## Lancer en local

Crée un fichier `.env` :

```env
BOT_TOKEN=ton_token_telegram
WEBAPP_URL=https://mrshop.astck.com
BOT_NAME=MRSHOP
```

Puis installe et lance :

```bash
pip install -r requirements.txt
python main.py
```
