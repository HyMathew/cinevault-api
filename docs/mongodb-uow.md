# Unité de travail MongoDB — lot ciblé

État : fondation implémentée pour les ports comptes, sessions et catalogue de
[contracts.py](../app/persistence/contracts.py), sans activation de routes REST.
Ce lot ne constitue pas un backend Enterprise complet : circulation, listes
privées, admission, audit, reçus et outils de migration restent des lots suivants.
Aucune double écriture ni aucun repli vers JSON n'est introduit.

## Utilisation et frontières

`MongoPersistence(database)` reçoit une base PyMongo déjà configurée et un client
vivant. Il ne se connecte pas à une URI implicite, ne provisionne pas la base et
ne prend pas possession du cycle de vie du client. Le driver reste une dépendance
optionnelle pour les consommateurs JSON. Aucun contrôleur HTTP n'est modifié.

`run(command)` réutilise le runner borné existant : snapshot, primary, majority,
fenêtre CSOT de dix secondes, trois tentatives transactionnelles et cinq commits
explicites au plus par tentative. Le callback peut être réexécuté. Il doit être
synchrone, produire un résultat calculé et ne provoquer aucun effet externe ; les
UUID/arguments durables d'une commande sont préparés une fois par son appelant.
Les erreurs de commit indéterminé conservent leur classe distincte, sans répétition
aveugle de la commande. Voir [le contrat du runner](persistence-uow.md).

La première opération d'une commande écrit seulement
`coordination/{_id: "business", revision: Int64}`. Sa révision incrémentée est
exposée par Work, sans setter public. Ce point de contention conserve une
sérialisation des commandes concurrentes ; il ne contient aucun tableau métier
et n'est pas un snapshot BSON global. La révision et les écritures sont annulées
ensemble en cas d'échec. Son épuisement Int64 est refusé, jamais remis à zéro.

`query(query)` ouvre une transaction snapshot sans écriture, lit la révision
courante puis les seules entités demandées. Les write methods sont refusées.
`readiness()` utilise cette voie pour vérifier coordination et transaction ; il
ne remplace pas le contrôle de provisionnement des schémas/index par maintenance,
ni une vérification de toutes les données. Une base absente n'est pas créée.

Toutes les opérations reçoivent explicitement la même session. Work et ses
repositories sont fermés à la fin de chaque tentative, succès ou exception, et
refusent leur usage depuis un autre thread/processus. Une instance du backend
peut servir plusieurs threads indépendants ; elle ne doit pas traverser un fork.
L'imbrication sur une même instance et les callbacks différés sont refusés avant
accès au stockage, avec le garde partagé par JSON.

## Repositories et validation ciblée

Les méthodes concrètes suivent exactement les trois ports existants : comptes
par UUID/username/page, sessions par empreinte et révocation/expiration explicite,
films/exemplaires par UUID et CRUD. Les lectures sont détachées, les entrées sont
encodées dans de nouveaux dictionnaires. Un save absent lève LookupError et ne
fait jamais un upsert. Les sélecteurs UUID, username, empreinte et pagination sont
validés avant requête ; aucun filtre fourni par un client n'est exposé.

L'identité UUID est revendiquée dans `entity_ids` et libérée dans le même commit
que son entité. Une collision intertypes est ainsi refusée ; une incohérence du
registre bloque la mutation, sans tentative de réparation silencieuse. Les dix
collections natives et ce registre ne constituent pas deux sources métier : le
registre ne contient que UUID et type, sans état de l'entité.

Les validateurs natifs assurent les formes, pas toutes les références ou relations
temporelles. Après la commande, les entités modifiées sont relues par identifiant.
Leurs fragments, avec le seul compte parent d'une session ou film parent d'une
copie, sont contrôlés par `validate_document` existant. Ces mini-documents sont
temporaires en mémoire : aucun chargement ni enregistrement du document global.
Cette validation conserve notamment `created_at <= last_seen <= expires_at`, les
formes canoniques et les références de sessions/copies.

Les prédicats finaux vérifient aussi les attentes d'un compte désactivé,
l'historique d'une copie déplacée/supprimée et toutes les références historiques
d'un film supprimé. Ils s'exécutent après la commande : une dépendance peut donc
être insérée plus tard dans la même UoW. Un futur service de désactivation pourra
annuler les attentes dans cette même transaction avant validation finale ; ce
port de circulation n'est pas encore implémenté ici.

Les erreurs d'unicité ou de validation native deviennent ValueError et condamnent
la tentative même si le callback tente de les masquer. Continuer puis committer
une transaction annulée par le serveur ne peut pas produire un faux succès.
Une exception annule l'ensemble ; un résultat métier normal reste un commit.

La recherche de doublon film filtre l'année, projette titre/directeur puis applique
le casefold Python existant. Le modèle n'acquiert pas de clés techniques nouvelles.
L'ajout d'un index sur l'année ou de clés normalisées exige une évolution mesurée
du modèle ; aucun gain de performance n'est revendiqué. L'ordre de retour de
`copies_for_movie` n'est pas un ordre métier stable promis par ce port. Un futur
consommateur qui en dépend doit définir cet ordre pour les deux adaptateurs.

La suppression de sessions est bornée en mémoire par lots de 500 identifiants ;
elle ne modifie pas l'expiration de sécurité. Celle-ci reste une décision de
service avec horloge et idle_ttl explicites, sans TTL MongoDB asynchrone. Les
révisions/entiers sont BSON Int64 : les valeurs hors de cette représentation sont
refusées sans perte de précision, comme documenté dans le [modèle](mongodb-model.md).

## Maintenance explicite

`mongo_maintenance.provision(database)` est réservé à une identité de maintenance,
sur une base candidate neuve ou déjà provisionnée. Il installe les dix schémas
natifs et 22 index nommés du modèle, plus les deux schémas stricts de coordination
et du registre ; les index `_id` automatiques sont supplémentaires. La révision
initiale est zéro uniquement quand le document de coordination n'existe pas.

L'opération ne fait ni collMod, ni drop, ni remplacement de données. Une forme,
action/level de validation, collation ou index nommé divergent provoque un refus.
Un index supplémentaire unique ou TTL non prévu est également refusé. Un état
métier préexistant sans coordination/registre requiert une migration séparée.
Relancer le provisionnement conforme ne change pas les entités ni une révision
déjà avancée ; une divergence n'est jamais corrigée par écrasement implicite.

Ce contrôle de configuration ne remplace pas l'inventaire/comparaison complète
d'une migration. Il ne certifie pas que des écritures d'opérateur ayant contourné
les validateurs auraient conservé toutes les références ou le registre. L'identité
applicative n'appelle pas provision et ne reçoit aucun privilège DDL de ce lot.

## Tests et preuves

`python -m pytest -q tests/test_mongo_persistence.py -W error` exécute les contrôles
locaux sans serveur et les tests réels si `MONGODB_TEST_URI` est défini. Sinon ces
derniers sont explicitement skipped ; ils ne passent pas sous mongomock.
L'URI doit désigner un environnement jetable avec permissions de création et de
suppression de ses bases synthétiques, jamais un magasin exploité.

Le même module de test s'importe sans pytest pour une sonde conteneur stdlib :
`exercise_backend(app_database, maintenance_database=maintenance_database)`
provisionne via maintenance et exerce le CRUD via application ;
`exercise_references(app_database)` vérifie histoire/FK et refus de masquage d'une
erreur native ; `exercise_concurrency(app_database)` fait concourir deux transactions
distinctes de rétrogradation d'administrateurs. Utiliser une nouvelle base jetable
par campagne, car ces fonctions créent volontairement des données synthétiques.

Checkpoint local du lot : 5 tests locaux réussis, 5 tests de vrai serveur non
exécutés faute d'URI ; suite API complète 288 réussis/5 skipped en 72,07 secondes,
Ruff check et format de 94 fichiers réussis, liens/documentation vérifiés offline.
Ces résultats ne valent ni preuve d'exécution MongoDB, ni migration, ni intégration
REST, ni déploiement ou garantie de haute disponibilité. Les preuves de vrai moteur
sont conservées séparément par leur orchestrateur avec l'identité exacte des sources.

Essai réel du 10 novembre 2026, 21:13:35–21:14:35 UTC : serveur MongoDB 8.0.34,
PyMongo 4.19.0 pure Python/Alpine, douze collections et deux identités distinctes.
Le compte de maintenance provisionne ; le compte applicatif n'a que CRUD sur ces
collections. Le contrat, huit mutations invalides, quatre gardes historiques et
la tentative de masquer une erreur native ont été éprouvés sans mutation partielle.
Une lecture catalogue observée n'accède qu'à coordination/movies, sans écrire ni
lire users/sessions. La première commande de mutation est findAndModify sur le
coordinateur, constatée via le vrai moniteur de commandes du driver.

Deux rétrogradations ont également été forcées à se chevaucher : la première
transaction attend le vrai refus WriteConflict 112 de la seconde avant de pouvoir
committer. Après reprise et relecture, résultats changed/last_admin, un seul
administrateur actif. Il s'agit de commandes internes du repository ; le test
REST concurrent reste un gate distinct. Création utilisateur/index et lecture
d'une autre base sont refusées au compte applicatif (13). Le provisionnement
refuse un validateur divergent et le laisse inchangé.

Manifeste des sources testées : SHA-256
`b20d7d1fa967fa276cd0fd194b7515877b5897e4c7e4178a5547dd5a8f3c283b` ;
reçu natif : `8812c06243a807c100f282064940ad27167cc275a5d601187687ee2c852aec5c`.
Programme, sources exactes et reçu sont conservés dans les artefacts privés du
projet. Un premier harnais avait confondu le paramètre update d'une commande
findAndModify avec son nom : corrigé par command_name, assertion conservée,
sans changement des sources candidates. Client, serveur et deux volumes neufs
supprimés après contrôle des propriétaires ; même API stable restée healthy.
