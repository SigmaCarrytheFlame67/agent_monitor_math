# Contribuer à Ansätze

[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md) · **Français**

Merci de contribuer à une recherche mathématique plus facile à examiner et à reproduire. Code, documentation, traductions, tests, améliorations de l’interface et exemples bien sourcés sont les bienvenus. Aucun accès au serveur de production ni compte de modèle payant n’est nécessaire pour commencer.

## Choisir une première tâche

| Domaine | Exemples | Où regarder |
| --- | --- | --- |
| Documentation et traduction | Clarifier l’installation, expliquer un graphe, synchroniser les trois langues. | [README](README.fr.md), [docs/](docs/) |
| Interface et accessibilité | Améliorer le focus clavier, les écrans étroits, les formules ou les états vides. | [agent_monitor/web/](agent_monitor/web/) |
| Exactitude des graphes | Reproduire une dépendance absente, distinguer recherche et déduction, tester une entrée cyclique. | [tests/](tests/) et modules de graphes dans [agent_monitor/](agent_monitor/) |
| Analyse et justification | Ajouter un cas minimal de parsing ou clarifier la portée d’un verdict. | [agent_monitor/analyze/](agent_monitor/analyze/) |
| CGM autonome | Améliorer contrats, orchestration, tests ou exemples hors ligne. | [cgm-framework/](cgm-framework/README.md) |
| Sources publiques | Corriger un lien ou un statut daté avec une source primaire et son attribution. | [Guide des sources](docs/problem-sources.md) |

Consultez les [issues existantes](https://github.com/ericjiang18/agent_monitor_math/issues) avant d’en ouvrir une. Ces exemples sont des pistes, pas une garantie de disponibilité d’une tâche. Discutez d’abord le besoin et le périmètre d’un changement important. Une petite correction peut faire directement l’objet d’une pull request.

## Signaler un bug ou proposer une fonctionnalité

Utilisez le [choix des modèles d’issue](https://github.com/ericjiang18/agent_monitor_math/issues/new/choose). L’anglais, le chinois simplifié et le français sont acceptés.

Pour un bug, précisez le commit ou la version déployée si vous les connaissez, le système et le navigateur, la vue concernée, les étapes de reproduction, le comportement attendu et observé, ainsi que des journaux **expurgés**. Préférez un petit exemple synthétique à une exécution privée volumineuse. Pour un défaut visuel, joignez une capture et les dimensions de la fenêtre.

Pour une fonctionnalité, décrivez la tâche de recherche, son utilisateur, l’obstacle actuel et un critère de réussite observable. Si elle modifie un graphe ou un statut de vérification, expliquez le sens des nœuds, arêtes et libellés.

Ne publiez aucun mot de passe, clé d’API, jeton, cookie, lien de partage privé, base de comptes ou manuscrit inédit sans autorisation. Un lien de partage peut accorder l’accès à lui seul, sans mot de passe.

## Signalements de sécurité

Ne publiez pas de détails exploitables dans une issue ordinaire. Utilisez **Security → Report a vulnerability** si le signalement privé est activé sur ce dépôt. Sinon, ouvrez une issue minimale demandant aux responsables d’organiser un canal privé, sans détails d’exploitation ni identifiants. Ne fournissez jamais un secret pour prouver une fuite : révoquez-le chez son fournisseur et décrivez seulement le type d’identifiant concerné.

## Préparer un clone de développement

Créez un fork, clonez-le dans un répertoire de développement, puis créez une branche :

```bash
git clone https://github.com/YOUR-USERNAME/agent_monitor_math.git
cd agent_monitor_math
git switch -c improve/short-description
```

Suivez l’[installation locale](README.fr.md#installation-locale). Si une autre copie sert la production, utilisez un clone séparé : modifier du HTML actif ou y lancer une compilation de production peut changer immédiatement le site. Simulez les fournisseurs si le changement ne nécessite pas d’appels réels.

Le CGM du site et le framework autonome sont deux implémentations distinctes. Exécutez les commandes du framework depuis `cgm-framework/`, dans son propre environnement ; modifier l’un ne modifie pas automatiquement l’autre.

## Respecter le périmètre du dépôt

Incluez le code, les ressources publiques, modèles de configuration, tests et documents liés à CGM, au suivi mathématique, à l’analyse de preuves et à ansatze.ai. Conservez les catalogues publics et leur provenance avec le code qui les utilise. Les scripts et entrées d’évaluation sont des sources ; les exécutions générées ne le sont pas.

Ne commitez pas de `.env` local, identifiants, bases de données, fichiers importés, espaces d’exécution, transcriptions privées, paramètres d’instance, dépendances installées, compilations, archives de manuscrits ou projets sans rapport. `data/` accueille l’état d’exécution ignoré ; seuls son README et les marqueurs de répertoires vides sont suivis. `.gitignore` ne remplace pas l’examen du diff.

Préférez de petites données synthétiques pour les tests. Présentez-les comme telles, sans les faire passer pour une exécution réelle ou un résultat expérimental vérifié. Préservez les licences et documentez les adaptations. Vérifiez les droits avant de redistribuer un manuscrit ou un jeu de données.

## Implémenter et vérifier

Traitez un problème cohérent à la fois. Suivez les conventions voisines, explicitez les états d’erreur et d’absence de données, et ajoutez un test de régression pour une correction de comportement significative. N’introduisez pas d’appel de modèle pour afficher un résultat enregistré.

| Zone modifiée | Contrôles pertinents |
| --- | --- |
| Console, API, tâches, analyse, graphes | Tests pytest ciblés, puis `python -m pytest tests/ -q` si nécessaire. |
| Logique navigateur | `node --test tests/*.mjs` et inspection de l’interface dans un navigateur. |
| Site public | `npm ci` et `npm run docs:build` dans un clone de développement. |
| Framework autonome | Depuis `cgm-framework/` : `python -m pytest cgm/ -q`. |
| README ou traductions | Vérifier les liens et images ; garder commandes, périmètre et limites cohérents dans les trois langues. |

Certains tests ouvrent des sockets de bouclage ou des sous-processus. Décrivez fidèlement les échecs liés à l’environnement ; un contrôle échoué ou ignoré n’est pas réussi. La matrice complète figure dans [Source checks](.github/workflows/ci.yml).

Pour une modification visuelle, examinez ordinateur et mobile, navigation au clavier, thèmes clair/sombre lorsqu’ils existent, chargement et absence d’artefacts. Joignez des captures avant/après avec des données synthétiques ou déjà publiques. Pour le partage, vérifiez la lecture seule et la compréhension des anciens instantanés.

## Préserver le sens des justifications

- **Explore** décrit recherche et exécution. Ses arêtes ne sont pas automatiquement des dépendances de preuve.
- **Facts** enregistre l’acceptation par modèle et ses dépendances ; ce n’est pas un certificat Lean.
- **Compilation Lean**, **contrôles d’axiomes** et **jugements de fidélité de l’énoncé** sont distincts. Préservez leur portée et les états sans résultat.
- **Analyze** rapporte des jugements de modèle sur un manuscrit. Compiler des énoncés formalisés ne les prouve pas.
- Les **instantanés partagés** sont des divulgations explicites et enregistrées. Leur lecture ne doit appeler aucun modèle, lancer aucun contrôle ni accéder à d’autres fichiers privés.

## Soumettre une pull request

1. Décrivez le problème concret et le comportement obtenu ; liez une issue associée si elle existe.
2. Indiquez les contrôles exécutés, leurs résultats et les limites pertinentes.
3. Ajoutez des captures avant/après pour les changements visibles, avec légendes et provenance exactes.
4. Mettez à jour documentation, traductions et attributions si nécessaire.
5. Examinez `git status`, `git diff --check` et le diff indexé. Retirez secrets, données privées et fichiers générés.
6. Ouvrez une pull request vers `main` ; un brouillon est bienvenu pour obtenir des retours précoces.

La revue porte sur la correction, l’utilisabilité, le périmètre, le sens des justifications et l’impact opérationnel. Répondez par des révisions ciblées. Une approbation de revue n’est pas un déploiement.

## Revue, fusion et publication

Les pull requests exécutent Source checks. Après fusion dans `main`, les contrôles sont relancés sur le commit fusionné. Le [service de déploiement périodique](deploy/ansatze/AUTO_DEPLOY.md), une fois installé, ne publie ce commit exact qu’après la réussite des jobs requis ; il réalise des contrôles de santé et permet un retour arrière. Une approbation seule ne publie pas du code non fusionné. Les contributeurs n’ont besoin ni d’identifiants de déploiement ni de jeton d’accès personnel GitHub.

## Travailler ensemble

Respectez les autres, expliquez les désaccords par des éléments concrets et distinguez la critique d’une idée de celle d’une personne. Accueillez des expériences mathématiques, techniques et linguistiques variées. Une contribution automatisée ou assistée par IA exige aussi un raisonnement lisible, des sources exactes, des vérifications utiles et un diff relu.

Consultez [ATTRIBUTION.md](ATTRIBUTION.md) et la licence du composant modifié. Conservez les notices existantes.
