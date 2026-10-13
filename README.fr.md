<p align="center"><img src="docs/public/readme/ansatze-icon.svg" width="60" alt="Ansätze" /></p>
<h1 align="center">Ansätze</h1>
<p align="center"><strong>Un espace de travail pour la recherche mathématique, l’exploration de preuves et l’examen de leurs justifications.</strong></p>
<p align="center"><a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <strong>Français</strong></p>
<p align="center">
  <a href="https://ansatze.ai/"><img src="docs/public/readme/ansatze-icon.svg" width="17" alt="" /> ansatze.ai</a> &nbsp;·&nbsp;
  <a href="https://github.com/ericjiang18/agent_monitor_math"><img src="docs/public/readme/github-icon.svg" width="17" alt="" /> Code source sur GitHub</a> &nbsp;·&nbsp;
  <a href="https://ansatze.ai/quickstart">Guide d’utilisation</a> &nbsp;·&nbsp;
  <a href="CONTRIBUTING.fr.md">Contribuer</a>
</p>
<p align="center"><a href="https://github.com/ericjiang18/agent_monitor_math/actions/workflows/ci.yml"><img src="https://github.com/ericjiang18/agent_monitor_math/actions/workflows/ci.yml/badge.svg?branch=main" alt="État des Source checks sur main" /></a></p>

Ansätze réunit **Continual Graph Memory (CGM, mémoire persistante sous forme de graphes)**, le suivi d’agents mathématiques, l’analyse de manuscrits et les éléments de vérification Lean dans une même console de recherche. Suivez une idée depuis l’énoncé jusqu’aux plans, tentatives de preuve, retours du vérificateur et résultats enregistrés. Consultez les graphes et les artefacts pour comprendre ce qui justifie une conclusion.

Ce dépôt contient le code de **[ansatze.ai](https://ansatze.ai/)**, le pipeline CGM à budget borné du site et **l’intégralité du framework de recherche CGM autonome**. Les deux implémentations disposent de leurs propres environnements et configurations.

> **Distinguez les niveaux de justification.** L’acceptation d’une étape par un modèle reste un jugement de modèle. Un plan de preuve, un graphe de recherche ou un énoncé compilé ne suffit pas à établir un théorème. La compilation Lean, les contrôles d’axiomes et la fidélité de l’énoncé formel au problème sont rapportés séparément.

<p align="center"><img src="docs/public/readme/ansatze-hero.png" width="720" alt="What if? — illustration mathématique avec des courbes orbitales bleu clair, des points reliés et des formules sur fond sombre." /></p>
<p align="center"><em>Et si ? — explorez les idées mathématiques avec Ansätze.</em></p>

**Sommaire :** [Fonctionnalités](#fonctionnalités) · [Par où commencer](#par-où-commencer) · [Interface](#guide-de-linterface) · [Installation locale](#installation-locale) · [CGM autonome](#cgm-autonome) · [Architecture](#architecture) · [Contribuer](#contributions-publiques) · [Déploiement](#de-la-pull-request-au-site-web)

## Fonctionnalités

| | Ce que vous pouvez faire | Ce que vous pouvez examiner |
| --- | --- | --- |
| 🧠 **Continual Graph Memory** | Coordonner un stratège, des agents de preuve, des vérificateurs par modèle distincts, des corrections bornées et un agent de curation. | Les dépendances des faits acceptés et l’historique d’exploration, dont les significations sont distinctes. |
| 📝 **Espace de preuve** | Partir de votre énoncé ou d’un problème public ; lire et poursuivre les exécutions prises en charge. | Le texte mis en forme, les artefacts, les références et les lacunes non résolues. |
| 🔎 **Analyse de manuscrits** | Importer les liens, textes ou fichiers pris en charge ; segmenter et examiner les résultats. | Le graphe du manuscrit, les verdicts locaux, leur synthèse selon les dépendances et le rapport. |
| 📊 **Suivi des agents** | Suivre l’exécution des moteurs de preuve pris en charge. | Les étapes, résumés des agents, consommations de tokens et coûts enregistrés, ainsi que les artefacts. |
| 🧩 **Intégration Lean** | Formaliser et vérifier des artefacts avec les outils Lean configurés et le service isolé. | Le code Lean, la compilation, les axiomes et les éléments distincts de fidélité de l’énoncé. |
| 🔗 **Partage en lecture seule** | Publier, actualiser ou révoquer un instantané explicite d’une exécution. | Sandbox, Monitor, les éléments Lean et les graphes disponibles : Outline, Explore, Facts, Formal, Compiler facts ou Manuscript. |
| 🗂️ **Site de recherche** | Parcourir les collections de problèmes, leurs sources et les graphes de recherche. | La provenance, les sources datées des statuts et les liens vers les documents d’origine. |

La disponibilité dépend du compte, des fournisseurs configurés, des moteurs installés et des artefacts enregistrés. Un contrôle absent n’est pas présenté comme réussi.

## Par où commencer

| Votre objectif | Point de départ |
| --- | --- |
| Essayer la console hébergée | Ouvrez [ansatze.ai](https://ansatze.ai/) et le [guide de démarrage](https://ansatze.ai/quickstart). L’accès invité dépend de la capacité et de la politique du service. |
| Comprendre l’interface | Lisez le [guide ci-dessous](#guide-de-linterface), puis le [guide complet](docs/quickstart.md). |
| Exécuter la console localement | Suivez l’[installation locale](#installation-locale). |
| Mener une recherche CGM autonome et persistante | Consultez le [guide du framework autonome](cgm-framework/README.md). |
| Améliorer le projet | Consultez les [contributions publiques](#contributions-publiques) et le [guide de contribution](CONTRIBUTING.fr.md). |
| Administrer le service hébergé | Lisez le [déploiement](deploy/ansatze/README.md), les [mises à jour automatiques](deploy/ansatze/AUTO_DEPLOY.md) et les [opérations](OPERATIONS.md). |

Les noms des vues restent en anglais pour correspondre à l’interface. Certains guides approfondis sont actuellement disponibles en anglais.

## Guide de l’interface

La console comporte quatre vues principales. **Sandbox** contient la preuve ou le rapport d’analyse. **Monitor** explique l’exécution enregistrée. **Lean Verification** affiche le code formel et les contrôles sauvegardés. **DAG** présente plusieurs vues de l’argument mathématique et du travail qui l’a produit.

| Vue DAG | Signification |
| --- | --- |
| **Explore** | Le processus de recherche : objectifs, plans, tentatives, retours du vérificateur et curation. Une arête ne représente pas nécessairement une implication logique. |
| **Facts** | Les énoncés admis par le processus de vérification par modèle configuré et leurs dépendances enregistrées. Cette admission n’est pas un certificat Lean. |
| **Outline** | Une vue informelle de la structure de la preuve. |
| **Formal · Lean** | Le graphe de preuve formelle sauvegardé, lorsqu’il existe. |
| **Compiler facts** | Les dépendances extraites de l’environnement du compilateur Lean configuré, si des éléments enregistrés valides sont disponibles. |
| **Manuscript** | Les résultats et références d’un manuscrit analysé, avec les verdicts du modèle enregistrés. |

En mode partage, toutes les vues sont **en lecture seule** et décrivent l’instantané enregistré. Le destinataire ne peut lancer ni appel de modèle ni vérification formelle. Un ancien partage peut ne pas contenir les graphes absents lors de sa création ; son propriétaire doit choisir **Create / update snapshot** pour inclure les vues disponibles. Révoquer un lien ne supprime pas les copies déjà enregistrées par un lecteur.

## Installation locale

### 1. Installer la console

Utilisez **Python 3.11–3.13** ; Python 3.13 correspond au job CI principal de la console. Linux est l’environnement de déploiement principal. Les agents externes et les services Lean ont des prérequis supplémentaires.

```bash
git clone https://github.com/ericjiang18/agent_monitor_math.git
cd agent_monitor_math
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
agent-monitor create-admin
./start.sh
```

Ouvrez **http://127.0.0.1:4600** et connectez-vous avec le compte administrateur local créé. La commande demande les identifiants de manière interactive ; ne passez pas de mot de passe dans les arguments du terminal. Si le port est occupé, utilisez `AGENT_MONITOR_PORT=4601 ./start.sh`.

### 2. Configurer un fournisseur

Configurez le fournisseur voulu dans le fichier `.env`, ignoré par Git. Les routes et limites disponibles figurent dans [.env.example](.env.example). Démarrer le serveur ne nécessite pas d’appel payant ; une preuve ou une analyse réelle peut entraîner des frais auprès du fournisseur.

Pour enregistrer les paramètres fournisseur d’un compte depuis l’interface, configurez une clé de chiffrement locale **avant de démarrer le serveur** :

```bash
mkdir -p .secrets
python - <<'PY'
import os
from pathlib import Path
from cryptography.fernet import Fernet
key = Path('.secrets/local-settings.key')
if not key.exists():
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(Fernet.generate_key())
PY
export AGENT_MONITOR_DATA_KEY_FILE="$PWD/.secrets/local-settings.key"
./start.sh
```

Arrêtez le serveur précédent avant de le relancer. Conservez cette clé avec votre état local privé : sa perte ou son remplacement empêche de déchiffrer les paramètres enregistrés avec elle. `.env`, `.secrets/` et les données d’exécution sont exclus de Git. Le répertoire d’état par défaut est `data/` ; `AGENT_MONITOR_DATA_DIR` permet d’en choisir un autre.

Le [script d’installation complet](setup.sh) peut installer des outils facultatifs et télécharger des moteurs amont. Lisez-le avant de l’exécuter. Les CLI d’agents de programmation exigent leur propre authentification ; les politiques de compte et d’exécution de la console restent applicables. Les [notes sur les sources externes](engines/EXTERNAL_CHECKOUTS.md) précisent ce qui n’est pas inclus dans le dépôt.

### 3. Démarrer le site public

Utilisez Node.js **22**, comme dans la CI du site, et npm. Exécutez ces commandes à la racine d’un clone de développement :

```bash
npm ci
npm run docs:dev
```

VitePress affiche l’adresse locale. Pour prévisualiser une compilation de production :

```bash
npm run docs:build
npm run docs:preview
```

Le site documentaire et la console Python sont deux processus distincts. La prévisualisation locale du site ne configure pas les comptes, les routes de modèles, le service Lean ni le proxy inverse de production.

## CGM autonome

Le framework complet se trouve dans **[cgm-framework/](cgm-framework/README.md)**. Il comprend les graphes persistants de faits et d’exploration, les services MCP par rôle, l’orchestration, la vérification, la consultation stratégique, les moteurs d’exécution, les contrats et compétences des agents, les exemples et les tests.

| CGM du site | Framework autonome |
| --- | --- |
| Pipeline à budget borné, limité aux réponses, exécuté dans les tâches de la console. | Projets de recherche persistants avec agents et services distincts. |
| Comptes, fournisseurs, annulation et budgets de la console. | Configuration, lanceurs, contrats et procédures propres. |
| Installé avec le paquet Python principal. | Installé séparément depuis `cgm-framework/`. |

Pour examiner le framework et lancer ses tests :

```bash
cd cgm-framework
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m cgm.orchestration --help
python -m pytest cgm/ -q
```

Le framework prend en charge Python 3.10+ et propose un [exemple hors ligne](cgm-framework/README.md#inspect-the-code-without-a-model-account) pour créer et examiner l’état d’un projet sans appeler de modèle. Pour des agents actifs, consultez le [démarrage](cgm-framework/docs/getting-started.md), la [configuration](cgm-framework/docs/configuration.md) et les [limites de confiance](cgm-framework/docs/security-and-trust.md). L’installation du framework autonome est facultative pour faire fonctionner le site.

## Architecture

![Architecture conceptuelle : un problème ou manuscrit entre dans la console ; CGM et Analyze produisent des artefacts enregistrés ; les vues exposent des justifications de nature distincte.](docs/public/readme/architecture.svg)

La console authentifie les requêtes et applique les politiques du compte, puis crée un espace par exécution et sélectionne un moteur. Le CGM du site coordonne les tentatives et enregistre les faits acceptés et l’exploration. Les autres adaptateurs convertissent leurs moteurs vers le même modèle d’exécution. Le moniteur rassemble événements, artefacts, consommation et graphes. Analyze examine un manuscrit existant ; les services Lean configurés vérifient séparément les artefacts formels.

| Chemin | Responsabilité |
| --- | --- |
| [agent_monitor/](agent_monitor/) | API, comptes, tâches, routage des modèles, analyse, partage et Lean. |
| [agent_monitor/cgm/](agent_monitor/cgm/) | CGM du site : stratège, agents de preuve, vérificateurs par modèle, corrections et curation. |
| [agent_monitor/web/](agent_monitor/web/) | Console interactive et lecteur de partage en lecture seule. |
| [cgm-framework/](cgm-framework/README.md) | Projet CGM autonome complet et sa documentation. |
| [monitor_core/](monitor_core/) | Traitement des événements et artefacts, graphes de dépendances et vues de suivi. |
| [docs/](docs/) | Site VitePress, composants Vue, catalogues publics, graphes de recherche et médias. |
| [engines/](engines/) | Sources des moteurs inclus et notes d’intégration ; certains moteurs nécessitent une installation séparée. |
| [deploy/](deploy/) | Caddy, systemd, mises à jour automatiques, passerelle fournisseur hébergée et service Lean isolé. |
| [tests/](tests/), [evals/](evals/README.md), [scripts/](scripts/) | Tests de régression, entrées d’évaluation, génération des catalogues et maintenance. |
| [problems/](problems/) | Manifeste et problèmes sources utilisés par la console. |

Le dépôt conserve le code public, les tests, les modèles de configuration et les ressources publiques nécessaires. Les exécutions privées, bases de comptes, fichiers importés, identifiants fournisseur, résultats d’évaluation générés et installations de dépendances ne font pas partie des sources suivies. La provenance des catalogues publics est décrite dans les [sources des problèmes](docs/problem-sources.md).

## Vérifications de développement

Avec les dépendances de développement de la console installées, exécutez à la racine :

```bash
python -m pytest tests/ -q
node --test tests/*.mjs
npm run docs:build
```

Lancez séparément les tests du framework depuis son sous-répertoire. Les tests utilisent des jeux de données locaux et des fournisseurs simulés ; certains démarrent des serveurs sur l’interface de bouclage ou des sous-processus. Le [workflow Source checks](.github/workflows/ci.yml) vérifie la console, le framework et le site. La réussite d’un test logiciel ne valide pas une affirmation mathématique.

## Contributions publiques

Les contributions sont les bienvenues en **English, 简体中文 et Français** : documentation, accessibilité, tests de régression, intégrations, sémantique des graphes, analyse de manuscrits, exemples reproductibles ou framework autonome.

- **Première contribution :** clarifiez une instruction, ajoutez un petit jeu de données synthétique ou corrigez une interaction au clavier ou sur mobile.
- **Signaler un bug :** indiquez la vue concernée, les étapes de reproduction, le comportement attendu et l’environnement, avec une capture expurgée ou un exemple minimal.
- **Proposer une fonctionnalité :** expliquez le travail de recherche visé et les justifications à afficher ; discutez les changements importants dans une issue avant de les réaliser.
- **Soumettre une pull request :** travaillez dans un fork et une branche, joignez des contrôles ciblés et des captures avant/après pour l’interface. Synchronisez traductions et attributions.

Commencez par le **[guide de contribution](CONTRIBUTING.fr.md)** · [English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md), puis [ouvrez une issue](https://github.com/ericjiang18/agent_monitor_math/issues/new/choose) ou [parcourez les pull requests](https://github.com/ericjiang18/agent_monitor_math/pulls). N’incluez aucune clé, aucun jeton d’accès, lien de partage privé ou manuscrit utilisateur dans une issue ou une capture publique.

## De la pull request au site web

```text
Pull request → Source checks → revue → fusion dans main
                                          ↓
                               Source checks sur main réussis
                                          ↓
                          tâche de déploiement → contrôles de santé
                                          ↓
                                   ansatze.ai mis à jour
```

Le [mécanisme de déploiement automatique](deploy/ansatze/AUTO_DEPLOY.md) nécessite une **installation initiale sur le serveur de production**. Une fois installé, il surveille le dépôt public et ne déploie le commit exact de `main` qu’après la réussite de tous les jobs Source checks requis. Le déploiement comprend des contrôles de santé et un retour arrière. Il ne nécessite ni jeton d’accès personnel GitHub ni secret SSH dans GitHub Actions.

L’approbation d’une revue ne fusionne pas la branche ; **la fusion dans `main` déclenche la publication**. Si les contrôles échouent, la version en ligne reste inchangée. Le guide explique l’activation, le suivi et la récupération. Compilez dans un clone de développement : modifier les sources actives ou reconstruire leur site peut affecter immédiatement la production.

## Questions fréquentes

<details>
<summary><strong>Un nœud de fait vert signifie-t-il que le théorème est formellement prouvé ?</strong></summary>

Non. Facts enregistre l’acceptation par le vérificateur par modèle configuré. La compilation Lean et la fidélité de l’énoncé apportent d’autres éléments, dont il faut comprendre la portée. Les déclarations d’énoncés compilées par Analyze ne fournissent pas leurs preuves.

</details>

<details>
<summary><strong>Pourquoi un graphe manque-t-il dans une exécution partagée ?</strong></summary>

L’instantané peut être antérieur à cette vue, ou l’exécution peut n’avoir jamais produit l’artefact correspondant. Le propriétaire peut l’actualiser avec les données disponibles. Le partage ne lance aucun modèle ni vérificateur pour combler un résultat absent.

</details>

<details>
<summary><strong>Faut-il installer tous les moteurs de preuve facultatifs ?</strong></summary>

Non. Configurez le moteur et le fournisseur souhaités. La présence d’un adaptateur ne garantit pas l’installation de ses programmes, environnements ou identifiants externes. Le projet CGM autonome dispose de sa propre procédure.

</details>

<details>
<summary><strong>Peut-on tester l’interface sans données privées ni appels payants ?</strong></summary>

Oui, avec les suites de régression et leurs données synthétiques. Excluez des contributions les données de compte privées et les liens donnant réellement accès à des partages privés.

</details>

## Attribution et licences

Consultez [ATTRIBUTION.md](ATTRIBUTION.md) pour les origines et les conditions de chaque composant. Le framework autonome conserve sa [licence Apache 2.0](cgm-framework/LICENSE) ; l’adaptation du site comprend [sa licence](agent_monitor/cgm/LICENSE) et [ses avis de modification](agent_monitor/cgm/NOTICE.md). Les moteurs, bibliothèques, polices et données publiques conservent leurs conditions respectives ; aucun badge unique ne les remplace.

La provenance des images et la notice de l’icône GitHub figurent dans les [notes sur les ressources](docs/public/readme/README.md). La marque GitHub identifie le lien vers le code source et n’implique aucun soutien au projet.
