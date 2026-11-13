# Réservations réalisées

Le [service commun de circulation C4](circulation-service.md) conserve ces règles
sur JSON/Mongo et documente les reçus optionnels des créations, les refus métier
qui peuvent committer l’expiration et les exceptions qui annulent toute l’UoW.
Évolution explicite de v2 : les nouvelles expirations groupées suivent sequence
croissante dans les deux moteurs, même si un ancien snapshot JSON a été réordonné.
Les événements antérieurs restent dans leur ordre sous la rétention déclarée2000.

Une attente est créée pour le compte authentifié seulement lorsqu'aucun exemplaire libre ne reste hors réservations. Une réservation par compte/film, dix attentes maximum ; quatorze jours avant expiration. L'ordre utilise une séquence affectée sous le verrou du document, sans ambiguïté pour deux demandes dans la même seconde.

Routes : POST/GET `/api/v1/reservations`, GET `/api/v1/reservations/{id}`, POST `/api/v1/reservations/{id}/cancel` avec If-Match. La liste accepte `status`, `movie_id`, `scope`, `limit`, `offset`. `scope=all` exige manager/admin ; `mine` est accessible à chaque rôle. Les membres voient leurs seules identités, positions et demandes associées ; aucun nom du reste de la file n'est envoyé.

La tête de file possède la priorité d'attribution. Un membre hors file ne peut utiliser qu'un exemplaire excédant le nombre d'attentes. L'approbation du prêt remplit la réservation correspondante dans la même transaction. `fulfilled` conserve le lien vers le prêt, y compris après retour. Les attentes terminées restent dans l'historique. Une annulation de réservation ne supprime pas une demande de prêt distincte.

L'expiration est vérifiée au traitement des réservations et de l'attribution ; l'affichage public ignore toujours les attentes expirées, même avant leur clôture persistée. L'audit d'expiration porte un acteur système null, permis uniquement pour cette action. Désactiver un compte annule ses attentes dans la transaction administrative et révoque ses sessions ; les prêts actifs restent à retourner effectivement.

`available = max(0, free - reserved)` ; `free` compte les exemplaires non abîmés et sans prêt actif, `reserved` compte les attentes non expirées. Ce dernier peut dépasser le nombre d'exemplaires. L'API publique n'envoie aucune identité d'emprunteur. Toute référence historique bloque une suppression silencieuse de l'œuvre.

Vérifications locales : tests de FIFO, identité, expiration avec horloge contrôlée, désactivation, schémas et deux processus créant la même attente (un 201, un 409). Le parcours HTTP Compose réel comprend création, ordre, tentative d'attribution hors priorité refusée, attribution à la tête, avancement du suivant, annulation et conservation après redémarrage. L'expiration à quatorze jours est testée avec horloge contrôlée, sans prétendre avoir attendu quatorze jours réels.

Le rendu navigateur des nouveaux écrans, les scans, l'audit indépendant final et le déploiement inter-VM restent à vérifier ; ces tests ne les remplacent pas.
