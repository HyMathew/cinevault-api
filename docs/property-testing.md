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

## P4 — circulation : plans symboliques et vraies limites

Le module [circulation.py](../tests/properties/circulation.py) utilise un oracle
de tickets nommés, une file ordonnée et un emplacement physique. Il décrit des
transitions contractuelles explicites, sans appeler les prédicats de priorité,
les fonctions d'expiration, les digests ou les ETags de production pour calculer
ses attentes. Les UUID/ETags renvoyés sont liés aux symboles comme valeurs opaques ;
la séquence de réservation est contrôlée strictement croissante, sans prédire sa
valeur technique. Les lectures de contrôle ciblent les identités connues, des
pages complètes bornées par chacun des quatre propriétaires de fixture et une
page d'audits bornée, pas un snapshot global Mongo. Le DTO CirculationQuery sert
seulement à ces lectures : aucun prédicat métier ne calcule les attentes. Le total,
les identités uniques, le champ propriétaire et le contenu de chaque page sont
comparés à l'ensemble exact attendu **pour cet acteur avant union**. Cela détecte
les créations parasites, mais aussi une page renvoyant toutes les entités connues
à chaque propriétaire. Les deux contrôles positifs initiaux injectent un prêt puis
une réservation valides hors du plan et exigent leur détection.

Les audits sont lus en pages de 100 dans la même lecture cohérente, jusqu'au total
retenu annoncé, dont la borne contractuelle de 2000 est vérifiée explicitement. Un
total supérieur, une page manquante ou des IDs dupliqués font échouer le contrôle :
il ne s'agit pas de qualifier silencieusement un préfixe incomplet. Les audits
attendus doivent rester dans cette rétention bornée ; si leur éviction rend
l'oracle incomplet, le test échoue plutôt que de purger ou ignorer l'historique.

Dans un JSON neuf isolé, **tous** ces audits sont comparés au plan, y compris un
audit système `actor_id=None` pour un ID inconnu. Sur un backend fourni/partagé, le périmètre
est celui de nos quatre acteurs **ou** des identités du plan : un audit émis par
notre acteur pour un ID inconnu est détecté, tandis que les expirations `actor_id=None` des
autres fixtures restent admises. Ce périmètre ne prétend pas auditer toutes les
actions étrangères du backend. Les témoins RED/GREEN supplémentaires injectent
les deux faux verts (audit inconnu et pages sans filtre propriétaire) et vérifient
le contraste avec une vraie expiration étrangère légitime, dont 125 audits
historiques de bruit placent nos événements au-delà de la première page.

| Campagne P4 | Budget vert | Contrats observés |
| --- | --- | --- |
| FIFO |25plans générés|Permutation des trois demandeurs, toutes combinaisons d'annulation, aucun saut/avant/à/après14jours ; tête contre suivant, dernier exemplaire occupé, retours successifs, fulfillment exact, options et historique.|
| Quotas |10plans générés|Onze films distincts ; dix demandes pending, cinq prêts actifs et dix attentes réels, refus du suivant puis libération et nouvelle admission. Aucun seuil réduit.|
| Reçus |6plans générés|Deux créations × avant/à/après24h ; namespace acteur-clé commun aux opérations, contenu divergent, clôture puis replay historique, ancienne identité conservée et nouvelle identité après TTL.|
| Régressions nommées |6exemples fixes|Même oracle, dont expiration commit sur refus métier403 et rollback sur exception de payload ; la réservation14jours expire après son reçu24h et une nouvelle création ne ressuscite jamais l'ancienne identité.|

Les plans FIFO réessaient les reçus de prêt lorsqu'il est actif puis retourné,
sans remettre l'entité vivante à son état pending historique. Chaque exemple
utilise un vrai fichier JSON neuf et le service de circulation commun. Le modèle
vérifie statuts, liens, unicité de l'exemplaire actif et ordre/acteur/timestamp de
chaque audit attendu aux étapes de contrôle. Les sessions après un saut temporel
sont renouvelées par fixture conforme aux bornes réelles ; ce n'est pas un test
de login. Les audits antérieurs ne sont ni purgés ni réécrits.

Les compteurs `circulation_fifo_json`, `circulation_quotas_json` et
`circulation_receipts_json` comptent les seuls exemples générés terminés et les
appels de service, pas les écritures de fixture ni les lectures d'invariants.
`circulation_corpus_json` compte un export complet de huit séquences comme un
exemple et conserve son nombre réel d'appels. Les régressions fixes ne gonflent
pas ces compteurs. La réduction Hypothesis est disponible sur le contrôleur ;
aucun nombre d'exemples ne constitue une preuve exhaustive.

```text
python -m pytest -q tests/properties/test_circulation_sequences.py tests/properties/test_circulation_corpus.py -W error --hypothesis-show-statistics
python tests/properties/export_circulation_corpus.py --output reports/circulation-native-corpus.json
```

L'export exige un nouveau fichier, utilise la graine20261113 par défaut et garde
son hash. Il contient deux plans FIFO, deux quotas, deux reçus, maintenance et
expiration du reçu de réservation, soit huit séquences. Le test exporte deux fois
avec la même graine, compare les octets puis exécute le corpus sur un vrai JSON
prépeuplé. Ce format séparé conserve intact le corpus P1-P3 à huit séquences.
Les cas sont intégralement validés avant accès à la persistance, y compris le
dernier cas invalide, les booléens à la place d'entiers et les budgets dépassés.

Sur une base native **jetable**, provisionnée avec les schémas exacts et le compte
applicatif, avec `tests/` dans `sys.path` :

```python
from properties.circulation_corpus import exercise_circulation_corpus

result = exercise_circulation_corpus(database, corpus)
```

L'interpréteur ne dépend ni d'Hypothesis ni de nouvelles dépendances externes ;
il utilise le runtime applicatif existant. La préparation Hypothesis reste sur
le contrôleur. Le reçu indique `generator_executed_here=false`,
`native_failure_shrinking=false` et `concurrent_engine_exercised=false`.
La maintenance peut expirer d'autres attentes de la base jetable au même instant :
ce corpus ne doit pas cibler une base stable. Son propriétaire nettoie la base
jetable. Cette entrée préparée n'est pas une exécution Mongo ; cette campagne
locale ne prétend pas fournir une preuve de concurrence moteur ou de panne réseau.

Quatre expériences C4 supplémentaires réutilisent le runner d'export immuable,
sans modifier les douze expériences P1-P3 : FIFO ignorée, expiration exclusive
à la frontière exacte, dixième demande pending rejetée et réponse historique du
reçu réécrite. Chacune doit échouer sur une assertion comportementale nommée,
sans erreur de collection/import ni skip ; le code applicatif du checkout reste
inchangé. Aucun score global n'est calculé.

```text
python tests/properties/circulation_mutation_gate.py --source-ref SHA_EXACT --output REPERTOIRE_NEUF_HORS_CHECKOUT
```

Les deux commandes de mutation restent séparées. Le workflow de propriétés
conserve le job à douze cas P1-P3 et ajoute un job à quatre cas de circulation,
avec ses patchs, JUnit, baseline et reçus propres. Les deux OS exportent aussi
le corpus P4. La commande générale `pytest tests/properties` découvre les tests
P4. Ce raccord doit encore réussir sa CI au commit exact ; sa présence dans le
workflow ne constitue pas une preuve d'exécution.

Le gate Mongo exporte les plans dans un processus contrôleur distinct, puis
utilise l'identité applicative du replica set jetable pour leur interprétation :

```text
python tests/properties/export_circulation_corpus.py --output reports/circulation-native-corpus.json
python scripts/check_mongodb.py --circulation-corpus reports/circulation-native-corpus.json
```

Le gate valide intégralement l'entrée bornée à 64 Kio avant toute opération
Docker, conserve son SHA-256 et refuse un replay incomplet ou un import Hypothesis
dans l'interpréteur. Sans cette option, ses contrats natifs historiques restent
disponibles. Le processus de génération peut utiliser Hypothesis installé dans
l'environnement de test ; le replay n'importe pas ce générateur.

## Qualification native locale P4 du 14 novembre 2026

Le [reçu natif](evidence/property-p4-native-20261114.json) et son
[manifeste de 85 sources](evidence/property-p4-native-source-20261114.json)
qualifient le code applicatif API7aa9b05 avec les deux helpers de replay du gel
P4 R2, SHA `7a9376752e65c93a6fd0c62b27353859253967dc954aae513b2c6672dbc3c78e`.
Le [corpus exact](evidence/property-p4-corpus-20261114.json), SHA
`4b34c2ac90d9b07ff6d8c1e6339fdf82022e41c1846d51f8c611f0f81c94d730`,
a exécuté **8 séquences et 149 appels de service sur MongoDB8.0.34 réel**.
Il vérifie les mêmes ensembles par propriétaire et audits retenus que JSON.
La génération reste sur le contrôleur ; shrinking natif et campagne P4
concurrente ne sont pas exécutés.

Les contrats C1-C4 et le corpus P1-P3 sont conservés dans cette même sonde,
dont sept courses entre processus appartiennent au contrat C4 distinct.
Les 153 secondes environ couvrent toute la sonde, sans constituer une mesure de
performance P4. Le driver, le serveur et les deux volumes possédés sont supprimés ;
un postcontrôle confirme le conteneur stable et son image inchangés, sains, sans
ressource d'essai restante. Le replica set à un membre n'apporte aucune redondance.
Les 89 fichiers de source, programmes et résultat sont conservés avec leurs
empreintes dans l'archive persistante de qualification.

Cette preuve utilise le pilote de sonde privé historique. Elle ne démontre pas
l'exécution du nouveau raccord CI public ci-dessus, encore distincte et à suivre.
La commande publique permet de reproduire le replay sans cet environnement privé.

## Périmètre restant

Le raccord CI P4 reste à qualifier au commit exact ; les mesures de
concurrence moteur restent dans les vrais tests dédiés. Migration JSON↔Mongo
(P5), admission équitable et capacité historique complète gardent leurs contrats
et chantiers propres. Aucune propriété décorative
sur un module non livré, aucun seuil métier réduit et aucune protection désactivée.
La publication ou un déploiement ne découle pas d'un succès local de ce lot.
