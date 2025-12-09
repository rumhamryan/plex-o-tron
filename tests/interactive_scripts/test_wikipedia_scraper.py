import asyncio
import sys
import os

# Add the project root to the Python path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "./")))

from telegram_bot.services.scrapers.wikipedia import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia,
)


async def main():
    print("Interactive testing for Wikipedia scrapers.")
    print("Enter 'exit' to quit at any time.")

    while True:
        try:
            mode = input("\nSearch for a 'movie' or 'tv' show? ").lower()
            if mode == "exit":
                break

            if mode == "tv":
                show_title = input(
                    "Enter show title (e.g., 'The Office (American TV series)'): "
                )
                if show_title.lower() == "exit":
                    break

                season_str = input("Enter season number: ")
                if season_str.lower() == "exit":
                    break
                season = int(season_str)

                episode_str = input("Enter episode number: ")
                if episode_str.lower() == "exit":
                    break
                episode = int(episode_str)

                print(f"\nFetching title for: '{show_title}' S{season}E{episode}...")
                (
                    episode_title,
                    corrected_title,
                ) = await fetch_episode_title_from_wikipedia(
                    show_title, season, episode
                )

                print("\n--- TV Show Results ---")
                print(f"Original Show Title: '{show_title}'")
                if corrected_title:
                    print(f"Corrected Show Title: '{corrected_title}'")
                else:
                    print("Show title was not corrected.")
                print(f"Season: {season}, Episode: {episode}")
                print(
                    f"Episode Title Found: '{episode_title}'"
                    if episode_title
                    else "Episode Title Not Found."
                )
                print("-----------------------\n")

            elif mode == "movie":
                movie_title = input("Enter movie title (e.g., 'Inception'): ")
                if movie_title.lower() == "exit":
                    break

                print(f"\nFetching years for: '{movie_title}'...")
                years, corrected_title = await fetch_movie_years_from_wikipedia(
                    movie_title
                )

                print("\n--- Movie Results ---")
                print(f"Original Movie Title: '{movie_title}'")
                if corrected_title:
                    print(f"Corrected Movie Title: '{corrected_title}'")
                else:
                    print("Movie title was not corrected.")

                if years:
                    print(f"Years Found: {', '.join(map(str, years))}")
                else:
                    print("No years found.")
                print("---------------------\n")

            else:
                print("Invalid mode. Please enter 'movie' or 'tv'.")

        except ValueError:
            print("Invalid number. Please enter integers for season and episode.")
        except Exception as e:
            print(f"An error occurred: {e}")


if __name__ == "__main__":
    asyncio.run(main())
