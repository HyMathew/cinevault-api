# Propriétés générées et expériences de mutation

Les tests sous [tests/properties](../tests/properties/) exercent des contrats
observables avec Hypothesis6.170.15. Ils complètent les exemples, courses et
contrats Mongo existants. Ils n'activent pas MongoDB pour les routes HTTP.

## Installation de test et provenance

`requirements-dev.txt` épingle Hypothesis et sortedcontainers2.4.0, sans extra.
Les [deux dépendances et leurs hashes](../tests/properties/dependencies/requirements-hashed.txt)
sont installables séparément avec `--require-hashes --only-binary=:all: --no-deps`,
puis le verrouillage dev complet est installé et vérifié par les gates existants.
La CI property audite également ces deux dépendances dans un venv qualité séparé.

Les wheels autorisées sont CPython3.13 Windowsamd64 et Linux glibc x86_64 pour
Hypothesis, et le wheel universel de sortedcontainers. Leurs licences complètes
issues des wheels sont conservées dans [dependencies](../tests/properties/dependencies/).
Hypothesis est sous MPL-2.0 ; sortedcontainers sous Apache-2.0. Cette attribution
ne change pas la licence MIT de CineVault. Pas de dépendance de test ajoutée à
l'image runtime. [Hypothesis officiel](https://pypi.org/project/hypothesis/),
[sortedcontainers officiel](https://pypi.org/project/sortedcontainers/).

Le wheel manylinux n'établit aucune compatibilité Alpine/musl. Le moteur génératif
s'exécute sur le contrôleur Windows ou Ubuntu équipé des bonnes wheels ; le
driver Alpine peut seulement exécuter l'interpréteur stdlib décrit plus bas.
Les deux jobs property/mutation ciblent Ubuntu24.04 : ils ne démarrent pas mongod.
Le contournement de noyau Ubuntu22.04 reste propre au gate serveur Mongo existant.
La disponibilité de wheels et cette configuration ne remplacent pas une CI réelle.

## Ce qui est généré et ce qui sert d'oracle

| Campagne | Budget par exécution verte | Frontière / oracle |
|---|---|---|
| Collections privées |25 séquences de20actions|Services finaux, vrai fichier JSON ; modèle indépendant de propriétaires, noms Unicode casefold, objets, versions opaques, pagination et audits minimaux.|
| Idempotence exemplaires |25 séquences de20actions|Services finaux, vrai fichier JSON ; modèle actor/clé/contenu, replay, conflit, suppression puis replay, renouvellement de session et expiration exacte à24h.|
| Précontrôle HTTP |25 séquences de20actions|Client Flask/routes JSON réels ;400/422/428/412/401/403/404 avant parsing sans écriture, succès à une commande, replay avec mêmeETag/Location.|
| Représentation BSON |100exemples|`encode_record → BSON.encode/decode → decode_record`, égalités strictes de valeurs et types ; aucun serveur nécessaire pour cette frontière.|
| Limites |25overflowInt64,25reservedkeys,5mauvais types exhaustifs|Refus au codec lorsqu'il s'agit de représentation ; sinon validation finale UoW et rollback du vrai fichier.|

Les vingt actions comprennent un préfixe explicite de transitions critiques et
une suite générée, pas vingt tirages indépendants sans état. Le modèle ne calcule
pas ses réponses avec les helpers métier/ETag/hash de production. Il associe aux
symboles les UUID réellement créés et conserve les ETags comme valeurs opaques.
Un remplacement identique ne doit pas nécessairement changer l'ETag.

Chaque exemple dispose d'un fichier frais dans son propre répertoire temporaire,
sans fixture mutable partagée entre exemples Hypothesis. Les noms custom générés
respectent le contrat interdisant caractères de contrôle et espaces aux bords ;
NUL/CR/LF/astral et texte Unicode arbitraire sont exercés dans les champs qui les
autorisent et dans BSON. Les cas invalides sont des tests séparés.

La frontière HTTP/précontrôle et celle de commande finale sont distinctes.
Un refus HTTP avant commande ne prolonge pas la session ; une commande finale
retournant un refus normal peut committer activité/révision. Les propriétés de
services vérifient alors la protection de l'état métier et la politique touch
existante, sans exiger arbitrairement l'identité complète du fichier. Une
validation levée dans l'UoW doit annuler tous ses effets.

Les UUID canoniques et références restent des **chaînes BSON**, y compris `_id`.
Le codec ne devient pas un validateur métier global : bool est une représentation
supportée mais ne convient pas à un champ entier ; float/datetime non supportés
par le codec sont refusés à cette frontière. Les champs illégaux injectés dans
une UoW sont refusés au commit validé. Les clés techniques réservées, leur
cohérence au décodage et l'ordinal d'audit positif caché restent protégés. Les
références historiques d'audit et de receipts vers un objet supprimé sont permises
par le contrat ; elles ne sont pas déclarées orphelines par un oracle trop large.

Une nouvelle session synthétique conforme aux vraies bornes8h/30min remplace les
anciennes après le saut24h des receipts. Cette préparation de fixture ne teste
pas un login par mot de passe et ne fabrique pas une session valable24h.

## Exécution et reçus

```text
python -m pytest -q tests/properties -W error --hypothesis-show-statistics --junitxml=reports/property-tests.xml
```

`reports/property-testing.json` conserve versions, état de sortie et nombres
réels d'exemples/actions terminés. Les compteurs d'une exécution en échec peuvent
comprendre les replays du réducteur et ne prouvent pas une campagne complète.
La sortie Hypothesis décrit aussi les exemples rejetés ou un domaine épuisé.
Le rapport peut être déplacé avec `PROPERTY_REPORT_PATH`. Le SHAHEAD ne suffit
pas à identifier des WIP : le handoff accompagne le reçu d'un manifeste de hashes.

Les deadlines par exemple sont désactivées localement pour les I/O sur vrais
fichiers, avec budgets et timeout de job bornés. Aucun health check n'est supprimé.
Le cache `.hypothesis/` est ignoré parGit ; un contre-exemple valide doit devenir
une régression explicite, sans élargir les préconditions pour le faire disparaître.
Ni tokens, ni mots de passe, ni URI de connexion ne sont enregistrés dans les
compteurs ou le corpus. [Séquences et réduction Hypothesis](https://hypothesis.readthedocs.io/en/latest/stateful.html).

## Corpus natif séparé : huit séquences de douze actions

```text
python tests/properties/export_corpus.py --output reports/property-native-corpus.json
```

Ce contrôleur génère quatre paires, puis exporte **huit séquences complètes** :
quatre privées et quatre receipts, douze actions chacune, préfixes compris dans
le JSON, soit96actions. La graine explicite par défaut20261112 rend cet artefact
reproductible ; les campagnes générales ci-dessus conservent leur exploration.
Le test corpus vérifie deux exports identiques puis exécute les96actions sur un
vrai fichier JSON déjà prépeuplé. La validation de tout le corpus précède les I/O.

Sur une base Mongo jetable provisionnée au snapshot exact, avec le compte
applicatif et `tests/` dans `sys.path`, l'entrée est :

```python
from properties.native import exercise_corpus
result = exercise_corpus(database, corpus)
```

Elle n'importe ni Hypothesis ni client HTTP et appelle les mêmes modèles/services.
Le résultat distingue48actions privées,48actions receipts et8séquences. Les
queries ciblent les comptes/sessions de fixture, leurs films/copies et une page
d'audit bornée ; aucun snapshot de toute la base. Les fixtures ont des UUID
propres et ne supposent pas une base vide. Le propriétaire de la base jetable
assure son nettoyage global ; ce helper ne purge pas d'historique stable.

Après les séquences, un **contrôle positif** insère le document users valide avec
created_at BSONInt64 sous les mêmes privilèges. La relecture exacte par UUID doit
préserver valeurs et types de tous ses champs, puis son nettoyage finally doit
supprimer exactement cet UUID. Le reçu n'annonce
`positive_control_native_valid_insert=true` que si ce contrôle est terminé.

Les trois essais négatifs conservent tous les autres champs du contrôle positif,
avec un nouvel UUID par essai, et remplacent seulement created_at par
bool/float/datetime : le helper exige le **code121** du validator natif, distinct
d'un refus de privilège/driver, puis nettoie uniquement chacun de ses UUID
synthétiques. Le hash de fixture satisfait le préfixe argon2id du schéma ; une
autre erreur de champ n'est donc pas présentée comme preuve du type entier.
Il ne prétend pas que le codec aurait validé globalement ces documents.

`test_native_control.py` vérifie localement ordre des commandes, témoin rejeté,
type relu incorrect, code13 non assimilé à121 et cleanup des UUID avec un recorder
de commandes. Ce test d'orchestration n'est ni un émulateur Mongo ni une exécution
du validator natif ; seule la sonde du vrai serveur qualifie cette preuve.

Le moteur Hypothesis et sa réduction ne tournent **pas** dans ce helper distant.
Il rapporte `generator_executed_here=false` et `native_failure_shrinking=false`.
Une erreur native conserve la séquence pour reproduction ; il ne s'agit pas d'une
réduction distante automatique. La CI property actuelle qualifie JSON/BSON et
exporte le corpus ; l'exécution Mongo de ce corpus doit être liée séparément au
snapshot/probe/compte réellement utilisés. Aucun skip ou mongomock ne vaut preuve.

## Douze expériences de mutation nommées

Le runner [mutation_gate.py](../tests/properties/mutation_gate.py) applique douze
patches unitaires explicites dans un export Git immuable hors du checkout actif.
Il n'exécute **pas mutmut**, ne nécessite pas fork et n'ajoute aucune dépendance de
mutation. C'est une expérience bornée choisie pour fonctionner aussi localement
sur Windows ; la CI la réexécute sur Ubuntu, pas une recherche automatique de tous
les mutants possibles. Il n'y a ni pourcentage global ni seuil artificiel.

Les défauts retenus sont : précontrôle committé ; lecture privée étrangère ; ETag
périmé accepté ; contenu de receipt ignoré ; receipt futur accepté ; expiration
inclusive erronée ; échéance absolue et idle inclusives ; horloge reculée acceptée ;
sixième vérification cryptographique ; audit conservant son préfixe ; petit entier
BSONInt32 au lieu d'Int64. Les régressions nommées partagent l'oracle des séquences.

```text
python tests/properties/mutation_gate.py --source-ref SHA_EXACT --output REPERTOIRE_NEUF_HORS_CHECKOUT
```

Le runner conserve archive/hash de source, hashes des tests superposés, patch exact
et son hash, baseline verte, sorties/JUnit par défaut, assertion requise et état
classé. Il désactive explicitement la conversion autocrlf de l'export puis vérifie
la restauration finale contre les blobs du commit. L'export est conservé pour
inspection ; aucun fichier applicatif du worktree actif n'est modifié.

Une détection exige un échec d'**assertion comportementale nommée**, zéro erreur
de collection/import, zéro skip et le codepytest attendu. Timeout, import,
ancre absente et autre erreur restent non classés/ouverts ; un retour vert est
survived. Le contrôle crypto utilise un provider de fixture espion : il démontre
l'absence de sixième appel, pas le coût ou la résistance d'Argon2. Sa vérification
enfin exécutée détecte aussi un appel indu suivi d'un rollback de validation.

La CI ne passe que si les douze expériences déclarées ont été détectées et si la
copie a été restaurée. Cela ne signifie ni couverture exhaustive par mutation,
ni mutation du driver réseau, ni preuve des courses ou pannes réelles. Une famille
future garde son statut non exécuté jusqu'à sa propre expérience.

## Périmètre restant

Prêts/réservations (P4), migration JSON↔Mongo (P5), admission équitable et historique
complet gardent leurs contrats et chantiers propres. Aucune propriété décorative
sur un module non livré, aucun seuil métier réduit et aucune protection désactivée.
La publication ou un déploiement ne découle pas d'un succès local de ce lot.
