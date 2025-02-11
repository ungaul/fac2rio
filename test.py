from mcrcon import MCRcon
import time

# Informations de connexion au serveur RCON
SERVER_IP = "127.0.0.1"  # Ou l'adresse publique si tu testes depuis une autre machine
RCON_PORT = 27015
RCON_PASSWORD = "HEIL"

# Fonction principale pour récupérer les joueurs connectés
def check_connected_players():
    try:
        with MCRcon(SERVER_IP, RCON_PASSWORD, port=RCON_PORT) as mcr:
            while True:
                # Envoie la commande RCON pour obtenir les joueurs connectés
                response = mcr.command("/players")
                print("Réponse du serveur :", response)

                # Pause de 20 secondes avant la prochaine vérification
                time.sleep(20)
    except Exception as e:
        print(f"Erreur lors de la connexion ou de l'envoi de la commande : {e}")

# Lancement du script
if __name__ == "__main__":
    check_connected_players()
