# Prochain lot : import/export du catalogue

**Conception à implémenter et tester ; aucune route de ce lot n'est encore annoncée comme disponible.** Le stockage et les opérations restent JSON, sans base de données supplémentaire.

## Périmètre et permissions

Gestionnaire ou administrateur seulement. Le transfert utilisateur porte sur les films et exemplaires partagés ; jamais comptes, hachages, sessions, collections privées, prêts, réservations ou reçus d'authentification. La sauvegarde du document complet est une opération d'exploitation distincte et confidentielle.

Un export cohérent est lu sous verrou. L'import valide toutes les lignes et prépare les changements avant de modifier le catalogue, puis vérifie doublons, identifiants et références dans la transaction. Aucun lot partiellement appliqué après une erreur de validation. Les droits sont relus dans la transaction.

## Formats

JSON versionné : `{format_version:1,movies:[...]}`, métadonnées de chaque œuvre et tableau de copies. Le schéma exact et les exemples seront figés avant le code. Les identifiants exportés sont conservables à l'import seulement si valides et sans collision globale. Ne jamais importer les autres tableaux du document interne.

CSV : en-tête fixe documenté pour titre, année, réalisateur, acteurs, genres, synopsis, support, emplacement et état. UTF-8, champs cités correctement, tableaux d'acteurs/genres encodés sans ambiguïté. Plusieurs exemplaires d'un film doivent être regroupés de façon déterministe ; une ligne sans exemplaire doit être distinguée d'un exemplaire aux champs incomplets. Fournir un fichier modèle et des tests de citations, accents et lignes multiples. La politique de séparateur devra être explicite, pas un mélange silencieux de formats.

L'export CSV doit neutraliser les cellules pouvant être interprétées comme formules par un tableur ; documenter cette transformation. JSON conserve le texte exact et constitue le format de transfert sans perte. Aucun contenu importé n'est exécuté ni évalué.

## Doublons et limites

Politique explicite `reject` par défaut : un conflit annule le lot entier. Option `skip` : lignes/œuvres déjà présentes ignorées avec compte rendu ; aucun remplacement silencieux des métadonnées ou exemplaires. Distinguer un doublon de film d'un exemplaire supplémentaire légitime. Définir et tester les collisions internes au fichier avant toute écriture.

Bornes de taille et de nombre d'œuvres/exemplaires à fixer et vérifier aux deux niveaux web/API. Les routes ordinaires gardent leur limite actuelle ; l'import possède une borne dédiée, pas une levée globale de protection. Encodage invalide, propriétés inconnues, types incorrects, JSON profond, NaN, colonnes manquantes/excédentaires et CSV malformé doivent échouer avec une réponse maîtrisée.

## Répétition et journal

Étendre les reçus avec une opération `catalog.import` et un résultat strict limité à l'identifiant d'opération et aux nombres créés/ignorés. Empreinte canonique liée à l'acteur, mêmes données/même clé → résultat initial ; clé réutilisée avec contenu différent → 409. Ne pas mémoriser le fichier brut ni des données d'authentification. La validation et le reçu appartiennent au même commit.

Journal minimal des imports/exports : acteur, type, résultat et identifiant d'opération, sans contenu de fichier ou nom personnel. Prévoir la trace des échecs de lot sans appliquer de changement métier. La forme de l'audit doit correspondre au schéma et aux tests, sans prétendre à une rétention illimitée.

## Sortie attendue

Pages Flask/Jinja d'import et téléchargement JSON/CSV, protection CSRF, rôle et état d'erreur avec instructions utiles. Tests unitaires, contrats REST, fichiers invalides sans mutation, doublons, deux imports concurrents, double soumission et parcours HTTP Compose réel. Les preuves doivent distinguer tests simulés et services réels.
