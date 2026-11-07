# Contrôles qualité reproductibles

Les environnements de test et de qualité sont séparés : `requirements-dev.txt` inclut l’application et pytest ; `requirements-quality.txt` verrouille les outils et leurs dépendances transitives. Python 3.13 est ciblé sur Windows et Linux. Installer ces fichiers dans deux environnements virtuels distincts ; leur dépendance jsonschema peut avoir une version différente.

## Commandes

Dans l’environnement de qualité :

```text
python -m ruff check .
python -m ruff format --check .
python scripts/check_docs.py
python scripts/audit_dependencies.py
python scripts/check_lock.py requirements-quality.txt
python -m pip check
```

Dans l’environnement de test : `python scripts/check_lock.py requirements.txt requirements-dev.txt`, `python -m pip check` puis `python -m pytest -q --junitxml=reports/tests.xml`. Les marqueurs et extras transitifs sont contrôlés contre les métadonnées installées. Le verrou qualité inclut aussi pip, requis par pip-api.

Le script de documentation vérifie les liens Markdown locaux dans README/docs, UTF-8 et leur maintien dans le dépôt. Il ne sonde pas les liens externes ni les fragments d’ancre. Pour l’API, il valide les exemples JSON actuellement disponibles. Les tests API comparent séparément le contrat à l’url_map Flask réellement enregistrée en mode Enterprise, y compris TRACE, HEAD explicite et OPTIONS explicite. Les HEAD automatiques associés à GET et OPTIONS automatiques sont facultatifs dans le contrat ; s’ils sont déclarés, leur présence réelle est vérifiée. Ce n’est pas un inventaire académique exhaustif des traitements ou de tous les exemples futurs.

API uniquement, environnement qualité : `python -m openapi_spec_validator docs/openapi-v1.json` valide le document complet.

## Images

```text
python scripts/check_image.py --image cinevault-api:local-audit
```

Le script construit l’image, vérifie son utilisateur et une exécution en UID non nul, attend le véritable HEALTHCHECK puis exporte l’image par son identifiant immuable fourni dans un iidfile propre au build, sans relire un tag mutable. Le démarrage de contrôle utilise un réseau désactivé, une racine en lecture seule, aucune capacité additionnelle et un /tmp éphémère. C’est une vérification de démarrage legacy ; les parcours Enterprise entre les deux services nécessitent leur test d’intégration distinct.

Grype 0.121.2 est exécuté depuis l’image officielle `anchore/grype` fixée au digest sha256:6a36234da13f9faa327306ac0b3dab3405cfa2b4eb20c8f904125a8707863913. Le scanner reçoit seulement une archive en lecture seule, sans socket Docker, dossier personnel ou clé hôte ; il télécharge sa base publique. La base et les temporaires du scanner utilisent un volume Docker Linux éphémère, en UID 65532. Un helper Python à base fixée au digest, sans réseau et avec la seule capacité CHOWN, prépare uniquement ce nouveau volume. Les noms uniques et deux labels de propriété sont revérifiés avant suppression. Les trois conteneurs de la tentative et ce volume sont nettoyés indépendamment, y compris après une expiration. Le rapport JSON conserve toutes les sévérités ; High/Critical ou une erreur technique font échouer le contrôle. Aucun avis n’est ignoré, aucune option ignore-unfixed. Une absence d’avis connu ne prouve pas l’absence de vulnérabilité.

Une tentative invalide les anciens reçus avant lancement ; ses erreurs techniques restent enregistrées. Chaque scan possède son propre sous-dossier et un reçu final de nettoyage.

Rapports sous `reports/`, exclus de Git et du contexte Docker : résultats d’audits runtime/dev/qualité, construction, santé, identifiant d’image et scan. La date/base du scanner et les versions doivent être conservées avec les résultats. Ces contrôles ne remplacent pas la revue du code, des droits et des secrets.

## GitHub Actions

Le workflow teste Ubuntu et Windows et audite sur chacun de ces OS les trois ensembles de dépendances, y compris les paquets conditionnels Windows. Il exécute aussi lint/format/docs/contrat puis construit et scanne l’image. Les actions officielles sont fixées à des commits, les permissions sont limitées à contents:read et les identifiants de checkout ne sont pas conservés. Pas de publication d’image ni déploiement automatique. Les rapports sont gardés sept jours, y compris lorsqu’un contrôle échoue.

La configuration du workflow et une exécution locale ne sont pas une réussite de CI distante. La publication n’intervient qu’après audit ; une exécution GitHub réelle devra être reliée à son commit et à son URL avant toute affirmation de réussite ou ajout d’un badge.

Sources des outils : [Ruff](https://docs.astral.sh/ruff/configuration/), [pip-audit](https://github.com/pypa/pip-audit), [Grype](https://oss.anchore.com/docs/installation/grype/).
