# Requêtes publiques du catalogue — lot C2c

Les quatre routes GET movies, movie, copies et copy utilisent maintenant
`PublicCatalogueService` et `Persistence.query`, avec `JsonPersistence` dans la
composition HTTP actuelle. Le service ne reçoit ni Flask, ni PyMongo, ni un
document global. L’accès JSON reste un vrai fichier validé ; aucune sélection
globale Mongo, migration ou seconde écriture n’est activée dans ce lot.

## Contrat conservé

Les filtres et leurs erreurs restent ceux du contrat public : q sous-chaîne de
`' '.join(title, director, *actors).casefold()`, genre exact après casefold,
année, présence d’un support, tri title/year et UUID comme second critère,
pagination limit1–100/offset0–10000, total avant pagination. Le tri descendant
inverse les deux critères. Les phrases entre deux champs restent recherchables.
Les caractères de regex sont des données littérales ; la normalisation ne dépend
ni de la collation linguistique Mongo ni de sa recherche plein texte.

La liste des copies GET n’a **aucune pagination ou troncature implicite**. Le
filtre movie_id mal formé conserve une liste vide ; les UUID de routes mal formés
reçoivent404. Les ressources et leurs ETag excluent toutes les clés techniques.
Les lectures ne touchent ni session ni révision. Les helpers historiques utilisés
par les domaines non encore extraits restent disponibles.

L’ordre des films est explicite et déterministe. Le contrat OpenAPI ne définit
pas d’ordre pour les copies ou supports de résumé : JSON conserve son ordre
historique d’insertion ; Mongo ne promet pas cet ordre de copie, et trie les
groupes de supports du résumé. Les mêmes ensembles, comptes et ressources sont
préservés, sans inventer une disponibilité à partir du simple nombre de copies.

## Accès ciblés MongoDB

Le port `search_movies(MovieQuery)` n’accepte qu’une valeur canonique typée ;
aucun dictionnaire de requête arbitraire ne vient de HTTP. Sans support, count et
find/tri/skip/limit se font côté serveur. Avec support, une jointure `$lookup`
ciblée par movie_id/support, limitée à un témoin, précède count et page. Toutes
ces opérations et les résumés partagent la transaction snapshot de lecture.

`copy_summaries(page_ids)` agrège sur le serveur par film/support, après `$match`
sur les seuls UUID de la page. Le client reçoit au plus500groupes pour100films et
cinq supports, au lieu d’un document par copie. Le service projette le résultat
sans lire les autres domaines. Les GET copies récupèrent volontairement toutes
les copies demandées, conformément au contrat existant ; c’est une limite de
volume de réponse documentée, pas un chargement global de la base métier.

La regex utilise `re.escape`, puis encode le NUL littéral en `\x00` pour conserver
les textes déjà admis sans NUL embarqué dans le motif transmis. Une recherche de
sous-chaîne non ancrée peut nécessiter un **COLLSCAN** : aucun gain de recherche
plein texte, Atlas Search ou index B-tree sur la sous-chaîne n’est revendiqué.

## Évolution du modèle et provisionnement

Les films ont quatre clés BSON dérivées du même calcul Python partagé avec JSON :
title_key, director_key, search_key et genre_keys. Elles sont réservées aux
adaptateurs, ajoutées à l’encodage, vérifiées puis retirées au décodage. Une clé
absente ou incohérente est refusée. Les limites tiennent compte d’une expansion
casefold jusqu’à trois caractères : title600, director360, search11853 et chaque
genre240. Les UUID, données publiques et entiers Int64 restent inchangés.

Trois index non uniques s’ajoutent aux22historiques : title_key/_id,
year/_id et year/title_key/director_key. Le dernier sert au prédicat de doublon
entièrement ciblé côté serveur ; il n’ajoute pas de nouvelle contrainte d’unicité
aux jeux JSON historiques. **25index déclarés**, plus les index `_id` automatiques.
La recherche genre seule n’acquiert pas d’index supplémentaire sans mesure.

Les validateurs nouveaux sont **incompatibles avec une ancienne base Mongo sans
ces clés**. Le provisionnement refuse une configuration divergente ; il ne fait
ni collMod, ni remplissage silencieux, ni désactivation de validation. Une base
existante requiert une migration explicite avant activation. Les anciennes preuves
de dix schémas/22index restent historiques ; elles ne valident pas ce nouveau
modèle. Le fichier JSON académique n’acquiert aucun de ces champs techniques.

## Vérification et limites

Le contrat `exercise_public_catalogue_service(database, observer=None)` utilise
le même service que les tests JSON. Il couvre casefold, regex littérale, NUL,
phrases entre champs, genres, support, tri, page/total, ETag, absence d’écriture et
plus de100copies. Les fixtures sont marquées pour cohabiter avec les autres lots
dans la base synthétique de CI. Il vérifie aussi le refus natif d’une clé absente,
puis le refus de lecture d’une clé altérée (fixture restaurée dans finally).

L’observateur optionnel est une liste de tuples `(command_name, collection)` :
il est vidé après création des fixtures, puis seules find/aggregate sur
coordination/movies/copies sont admises pendant les fenêtres de lecture. La CI
attache un vrai CommandListener au client applicatif et vérifie cette trace ;
aucun filtre, résultat, identifiant utilisateur ou secret n’y est conservé.

Le helper appelle réellement explain/queryPlanner pour trois formes
représentatives (sous-chaîne, année+ordre, doublon) et retourne uniquement les
stages/index gagnants observés. Les mesures sur cette petite fixture ne sont pas
un benchmark ni une preuve de capacité. Tant que ce helper n’a pas été exécuté
sur le nouveau snapshot natif, ses plans sont **non mesurés** : son existence et
les tests sans serveur ne sont pas une preuve moteur. La CI Mongo permanente
exécute le helper ; les résultats et limites restent à lire dans le reçu exact.

Sources primaires : [regex](https://www.mongodb.com/docs/manual/reference/operator/query/regex/)
et [lookup](https://www.mongodb.com/docs/manual/reference/operator/aggregation/lookup/).

## Sonde native exécutée le 11 novembre 2026

De18:21:33 à18:22:38UTC, le même helper a réussi sur MongoDB8.0.34 réel, avec
identités de maintenance et applicative séparées. Le snapshot immuable de53fichiers
porte l’empreinte de manifeste
`1a073a5b9d2c86be33b349ed34ef476151cedad3a3e4af4be436ee5945cb4c59`.
Les57commandes de lecture observées restent sur coordination/movies/copies,
sans mutation ni accès aux comptes ou sessions. Les copies au-delà100 sont
retournées, le validateur refuse la clé absente avec code121 et le décodage refuse
la clé altérée. Les contrats d’authentification, films et exemplaires ont aussi
réussi sur ce snapshot.

Les trois plans gagnants réellement observés sur les fixtures synthétiques sont :

| Requête | Étapes observées | Index |
| --- | --- | --- |
| Sous-chaîne littérale | COLLSCAN | Aucun |
| Année et ordre | FETCH, IXSCAN, LIMIT | movie_year_order |
| Prédicat de doublon | FETCH, IXSCAN | movie_natural_key |

Ce sont des plans sur un petit jeu, sans mesure de capacité ni garantie de choix
du plan pour d’autres données. Les deux conteneurs et deux volumes de la sonde
ont été nettoyés ; l’API stable reste saine. Cette preuve ne constitue toujours
pas une activation HTTP Mongo, une migration ou un déploiement.
