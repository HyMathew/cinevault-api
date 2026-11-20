# Statistiques et recommandations

Les deux routes interrogent le dépôt de lecture des statistiques et
recommandations pendant la transaction qui vérifie et actualise la session.
Une erreur de paramètres annule cette transaction. Un membre peut consulter
ses propres indicateurs. La vue opérations est réservée aux gestionnaires et
administrateurs et ne contient que des totaux.

MongoDB regroupe les exemplaires par film et support. La disponibilité retire
les exemplaires endommagés, les prêts actifs et la part du stock libre qui
correspond à des réservations en attente encore valides. Les lectures de
collections privées, d'activité et de signaux de recommandation sont filtrées
par le compte de la session. La vue opérations agrège les historiques sans
transmettre les identités des emprunteurs.

Le classement parcourt le catalogue et conserve les 24 premiers résultats
au maximum. Il applique les mêmes scores, motifs et départages à JSON et à
MongoDB. MongoDB ne calcule ensuite la disponibilité que pour les films
retenus. Le scan des films reste linéaire. Le calcul de stock global de la
page statistiques doit être mesuré sur les volumes prévus.

Le portage a été vérifié par les tests HTTP existants de confidentialité,
d'expiration, de score et de schéma. Les deux nouveaux tests empêchent le
retour à la route documentaire et l'agrégation de stock pour les films
écartés du classement. La qualification native du lot intégré reste à
exécuter.
