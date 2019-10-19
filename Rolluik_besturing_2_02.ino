/* Arduino code - To use a single pulse to cycle through a number of choices. This code uses functions to:
** debounce the button
** check the state of the button
** keep track of the current selection of the pulse (in this case 0, 1, 2, 3)
** cycle to output relays that correspond an action of the shutter, open, pause, close, pause.
*/

// global variables
const int buttonPin =  2;

boolean currentState = LOW;
boolean lastState    = LOW;
boolean stateChange  = false;

int currentButton = 0;
int lastButton    = 4;
int relaisArray[]    = {9, 10, 11, 12};
unsigned long previousMillis = 0;
long interval = 1000 ;

// setup
void setup() {
  pinMode(buttonPin, INPUT);
  for (int i=0; i<5; i++){
    pinMode(relaisArray[i],OUTPUT);
  }
  Serial.begin(9600);
}

// main loop
void loop(){
  currentState = debounceButton();
  stateChange = checkForChange(currentState, lastState);
  currentButton = getButtonNumber(lastButton, currentState, stateChange);
  indicatorLight(currentButton);
  lastState  = currentState;
  lastButton = currentButton;
}


// function debounceButton
boolean debounceButton()
{
  boolean firstCheck   = LOW;
  boolean secondCheck  = LOW;
  boolean current = LOW;  
  firstCheck  = digitalRead(buttonPin);
  delay(50);
  secondCheck = digitalRead(buttonPin);  
  if (firstCheck == secondCheck){
    current = firstCheck;
  }
  return current;
}

// function checkForChange
boolean checkForChange(boolean current, boolean last)
{
  boolean change;  
  if (current != last){
    change = true;
  }
  else {
  change = false;
  }  
  return change;
}

// function getButtonNumber
int getButtonNumber(int button, boolean state, boolean change)
{ 
  if (change == true && state == LOW){
    button++;
    if (button > 3){
      button = 0;
    }
    Serial.println(button);
  }
  return button;
}

// function indicatorLight
void indicatorLight(int button)
{
  for (int i=0; i<4; i++) {
    digitalWrite(relaisArray[i], LOW);
  }
  digitalWrite(relaisArray[button], HIGH);

}
