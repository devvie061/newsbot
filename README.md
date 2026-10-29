# NewsBot (version cloud, GitHub Actions)

NewsBot surveille Google News pour tes mots-clés et t'envoie une alerte Telegram quand
au moins 3 médias différents parlent de la même histoire. Cette version tourne entièrement
sur les serveurs de GitHub, gratuitement : ton ordinateur peut rester éteint.

---

## Installation de A à Z (environ 15 minutes)

### Étape 1 : créer ton bot Telegram

1. Dans Telegram, cherche **@BotFather** (avec le badge bleu) et ouvre la conversation.
2. Envoie `/newbot`.
3. Donne un nom au bot (par ex. `Mes alertes animaux`), puis un identifiant qui finit par
   `bot` (par ex. `alertes_animaux_max_bot`).
4. BotFather répond avec un **token** du type `123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`.
   Copie-le dans une note : c'est le mot de passe de ton bot, ne le partage pas.
5. Clique sur le lien `t.me/...` donné par BotFather pour ouvrir ton bot, mais **n'envoie
   rien pour l'instant**.

### Étape 2 : créer un compte GitHub

Va sur [github.com/signup](https://github.com/signup) et crée un compte gratuit (e-mail,
mot de passe, pseudo). Ton pseudo apparaîtra dans l'adresse de ta page d'historique.

### Étape 3 : créer le dépôt

1. Une fois connecté, clique sur **+** en haut à droite, puis **New repository**.
2. **Repository name** : `newsbot`.
3. Choisis **Public**. C'est ce qui rend GitHub Actions gratuit et illimité. Tes clés
   resteront secrètes (étape 5), mais tes mots-clés et ton historique seront visibles.
4. Ne coche rien d'autre et clique sur **Create repository**.

### Étape 4 : envoyer les fichiers

1. Sur la page du dépôt vide, clique sur le lien **uploading an existing file**.
2. Ouvre le dossier `newsbot-cloud` dans le Finder et appuie sur **Cmd + Maj + .** (point)
   pour afficher le dossier caché `.github`.
3. Sélectionne les 4 éléments, `.github`, `config.json`, `newsbot.py` et `README.md`, puis
   glisse-les dans la page GitHub.
4. Clique sur **Commit changes**.
5. Vérifie que le dépôt contient bien le dossier `.github/workflows` avec `newsbot.yml`
   dedans. Sinon, voir « Le dossier .github n'est pas passé » dans le dépannage, plus bas.

### Étape 5 : enregistrer tes clés secrètes

1. Dans le dépôt, va dans **Settings**, puis **Secrets and variables › Actions** dans le menu de gauche.
2. Clique sur **New repository secret** :
   - **Name** : `TELEGRAM_TOKEN`
   - **Secret** : le token de l'étape 1
   - **Add secret**
3. Optionnel : si tu veux que Claude vérifie les alertes et les résume en français (environ
   1 centime par alerte), ajoute un deuxième secret `ANTHROPIC_API_KEY` avec une clé créée
   sur [console.anthropic.com](https://console.anthropic.com/).

Les secrets ne sont jamais visibles, même dans un dépôt public.

### Étape 6 : lier le bot et lancer le premier scan

1. Dans Telegram, envoie **`/start`** à ton bot. Fais-le maintenant et toi-même : la première
   personne qui envoie `/start` devient la propriétaire du bot.
2. Sur GitHub, ouvre l'onglet **Actions** du dépôt, clique sur **NewsBot** à gauche, puis sur
   **Run workflow** et encore **Run workflow**.
3. Après 1 à 2 minutes, Telegram affiche **« ✅ NewsBot est connecté »**, puis les premières
   alertes (le premier scan couvre les dernières 24 h).

À partir de là, GitHub relance le bot automatiquement toutes les 5 minutes.

### Étape 7 : activer la page d'historique

1. Dans **Settings**, ouvre **Pages** dans le menu de gauche.
2. Sous **Build and deployment › Source**, choisis **Deploy from a branch**.
3. Choisis la branche **gh-pages** et le dossier **/ (root)**, puis **Save**.
4. Après une minute, ta page est en ligne sur `https://TON-PSEUDO.github.io/newsbot/`.
   Elle se met à jour après chaque alerte.

C'est terminé.

---

## Utilisation

### Commandes Telegram

| Commande | Effet |
|---|---|
| `/liste` | Mots-clés et réglages actuels |
| `/ajouter lion, red panda` | Ajoute des mots-clés (virgules pour les expressions de plusieurs mots) |
| `/retirer cat` | Retire un mot-clé |
| `/seuil 4` | Nombre minimum de sources distinctes (2 ou plus) |
| `/histoires` | Les 10 dernières histoires signalées, avec le lien vers la page d'historique |
| `/histoire 12` | Tous les articles de l'histoire #12 |
| `/statut` | Heure du dernier scan |

Dans cette version cloud, le bot ne lit tes messages qu'à chaque passage : **la réponse peut
mettre 5 à 20 minutes à arriver**. C'est normal.

### Modifier les réglages sur GitHub

Ouvre `config.json` dans le dépôt, clique sur le crayon ✏️, modifie puis **Commit changes**.
Le changement est pris en compte au passage suivant. Les réglages sont décrits ci-dessous.

| Clé | Défaut | Rôle |
|---|---|---|
| `keywords` | dog, cat, elephant | Mots-clés surveillés |
| `min_sources` | 3 | Sources distinctes nécessaires pour une alerte |
| `renotify_new_sources` | 3 | Nouvelles sources nécessaires pour envoyer une mise à jour |
| `similarity_threshold` | 0.3 | Plus bas = regroupe davantage ; plus haut = plus strict |
| `story_window_hours` | 48 | Durée pendant laquelle un article peut rejoindre une histoire |
| `max_alerts_per_scan` | 10 | Évite les rafales ; le reste part au passage suivant |
| `blocked_domains` | facebook.com, youtube.com… | Sources ignorées |
| `google_news` | en-US / US | Langue et pays de Google News |

(`interval_minutes` ne sert pas ici : c'est GitHub qui décide du rythme, voir le fichier
`.github/workflows/newsbot.yml`.)

---

## Bon à savoir

- **Rythme réel.** GitHub planifie toutes les 5 minutes, mais retarde souvent les passages
  quand ses serveurs sont chargés. Compte plutôt 10 à 20 minutes, avec parfois un passage
  sauté. Le bot rattrape toujours la période manquée.
- **Inactivité.** Sur un dépôt public, GitHub peut suspendre les tâches planifiées après
  60 jours sans activité. Il t'envoie alors un e-mail. Pour relancer : onglet **Actions ›
  NewsBot › Enable workflow**.
- **Conditions d'utilisation.** Google réserve ses flux RSS à un usage personnel et non
  commercial, ce qui est ton cas. Côté GitHub, Actions est prévu en priorité pour le
  développement de logiciels. Un petit bot personnel comme celui-ci est très courant, mais
  ce n'est pas l'usage principal prévu. Si un jour GitHub le refuse, la version serveur
  (environ 4 €/mois) prend le relais avec le même code.
- **Ce qui est public.** Le code, `config.json` (tes mots-clés), l'historique (branche
  `data`) et la page d'historique. Tes tokens et clés, eux, restent secrets.
- **Coût.** 0 € avec GitHub. Si tu ajoutes Claude, environ 1 centime par alerte.

## Dépannage

| Problème | Solution |
|---|---|
| Rien n'arrive sur Telegram | Onglet **Actions** : clique sur le dernier passage. Une croix rouge ✗ indique une erreur, ouvre l'étape en rouge pour lire le message. |
| « Telegram … Unauthorized » dans le journal | Le token est faux : recrée le secret `TELEGRAM_TOKEN` (étape 5). |
| Le bot a été lié à la mauvaise conversation | Récupère ton identifiant de conversation auprès de @userinfobot dans Telegram, puis ajoute-le en secret `TELEGRAM_CHAT_ID`. |
| « Google News ne répond plus » | Google limite parfois les serveurs de GitHub. Le bot réessaie tout seul ; si ça dure plusieurs heures, passe à la version serveur. |
| Le dossier `.github` n'est pas passé | Dans le dépôt : **Add file › Create new file**, nomme-le `.github/workflows/newsbot.yml`, colle le contenu du fichier `newsbot.yml`, puis **Commit changes**. |
| Erreur « Permission denied » au moment du push | **Settings › Actions › General › Workflow permissions** : choisis **Read and write permissions**, puis **Save**. |
| Mettre le bot en pause | **Actions › NewsBot › ⋯ › Disable workflow** (et **Enable workflow** pour reprendre). |
